import argparse
import base64
import io
import logging
import math
import os
import re
import sys
import tempfile
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import ocha_stratus as stratus

from src.constants import COUNTRY_LIST_TAG, LAC_ISO3S, TEST_LIST_IDS
from src.data import (
    fetch_active_storm_meta,
    fetch_adam_current_exposure,
    fetch_adam_current_exposure_adm1,
    fetch_adam_historical_exposure,
    fetch_admin_population,
    fetch_all_prior_country_pairs,
    fetch_buffers,
    fetch_current_obsv_exposure,
    fetch_current_obsv_exposure_adm1,
    fetch_fcast_exposure,
    fetch_fcast_exposure_adm1,
    fetch_fm_names,
    fetch_gdacs_current_exposure,
    fetch_gdacs_current_exposure_adm1,
    fetch_gdacs_historical_exposure,
    fetch_historical_obsv_exposure,
    fetch_lookup_caveats,
    fetch_track_geo,
    fetch_prev_any_pairs,
    fetch_wsp_fcastonly_exposure,
    fetch_wsp_fcastonly_polygons,
    load_adm0_boundaries,
    load_adm1_boundaries,
    load_background_countries,
)
from src import fm_matching as fm
from src.landfall import compute_landfalls, nearest_adm1_name, saffir_simpson
from src.preview import PreviewUnavailable, render_with_template
from src.xlsx_style import build_readme, style_data_sheet
from src.plots import (
    StormMark,
    WspPdf,
    country_strip_chart,
    track_plot_buffers,
    track_plot_exposure,
    track_plot_wsp,
    wind_speed_color,
)

_HIST_COLOR = "#888888"
_SRC_LABELS = {"our": "CHD", "ADAM": "ADAM", "GDACS": "GDACS"}

# Column order for the per-storm exposure workbook tabs. Long format, one row
# per (admin unit, wind threshold) — the same layout and identity columns as
# the historical archive workbook (ds-storm-impact-harmonisation
# build_exposure); only the value block differs: a single MAX-across-sources
# pop_exposed (+ sources/caveat), vs the archive's three per-source columns.
_ADM0_COLS = [
    "atcf_id", "storm_name", "season", "admin_level", "iso3", "admin_name",
    "is_final_alert", "wind_speed_kt", "sources", "pop_exposed",
]
_ADM1_COLS = [
    "atcf_id", "storm_name", "season", "admin_level", "iso3", "country_name",
    "admin_pcode", "admin_name", "is_final_alert", "wind_speed_kt", "sources",
    "pop_exposed", "caveat",
]

# caveat_kind → readable adm1 alignment policy for the caveats tab (same
# controlled vocabulary as the archive workbook's build_caveats).
_ALIGN = {
    "country_only": "national-only (adm1 from CHD)",
    "no_fm_source": "national-only (no FieldMaps boundary)",
    "no_adam_source": "national-only (no FieldMaps boundary)",
    "fm_adm1_only": "national-only (source has no comparable adm1)",
    "needs_manual_mapping": "partial / manual (boundary-vintage mismatch)",
}
_CAVEAT_TAB_COLS = ["source", "iso3", "country_name", "scope",
                    "adm1_alignment", "caveat_kind", "caveat_note", "note"]

# WSP probability band midpoints (fraction) used to compute expected exposure.
_WSP_BAND_MIDPOINT = {
    0: 0.025, 5: 0.075, 10: 0.15, 20: 0.25, 30: 0.35,
    40: 0.45, 50: 0.55, 60: 0.65, 70: 0.75, 80: 0.85, 90: 0.95,
}


def _wsp_expected_pop(
    wsp_exp_df, atcf_id: str, iso3: str, wind_threshold_kt: int
) -> float | None:
    """Probability-weighted expected population exposed from WSP fcastonly bands.

    Returns None if no WSP data exists for this (atcf_id, iso3, wind_threshold_kt).
    """
    sub = wsp_exp_df[
        (wsp_exp_df["atcf_id"] == atcf_id)
        & (wsp_exp_df["iso3"] == iso3)
        & (wsp_exp_df["wind_threshold_kt"] == wind_threshold_kt)
    ]
    if sub.empty:
        return None
    return sum(
        _WSP_BAND_MIDPOINT.get(int(row["percentage"]), 0.025) * int(row["pop_exposed"])
        for _, row in sub.iterrows()
    )


def _storm_label(name: object, season: object, suffix: str = "") -> str:
    """Build a strip-chart label.

    Historical (no suffix): "Storm 2024" — single line including year.
    Current (suffix given): "Storm\\nsuffix" — two lines, year dropped to keep
    the visual compact.
    """
    name_ok = isinstance(name, str) and name and not (
        isinstance(name, float) and math.isnan(name)
    )
    base = name.strip().title() if name_ok else "Unknown"
    if suffix:
        return f"{base}\n{suffix}"
    season_ok = (
        season not in (None, "")
        and not (isinstance(season, float) and math.isnan(season))
    )
    season_part = f" {int(season)}" if season_ok else ""
    return f"{base}{season_part}"


_ET = ZoneInfo("America/New_York")


def _format_issued_et(dt: datetime) -> str:
    """Format an issued time (naive = UTC) in US Eastern, e.g. 'Jun. 6, 11am'."""
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    local = aware.astimezone(_ET)
    hour = local.strftime("%I").lstrip("0") or "12"
    ampm = local.strftime("%p").lower()
    return f"{local.strftime('%b')}. {local.day}, {hour}{ampm}"


def _oxford(items: list[str]) -> str:
    """Join with an Oxford comma: [] → '', [a] → 'a', [a,b] → 'a and b',
    [a,b,c] → 'a, b, and c'."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _build_subject(
    issued_time_dt: datetime, storm_names: list[str], prefix: str = ""
) -> str:
    """[Cyclone monitoring] NHC forecast issued {ET time} (storm, storm)."""
    names = ", ".join(storm_names) if storm_names else "—"
    return (
        f"{prefix}[Cyclone monitoring] NHC forecast issued "
        f"{_format_issued_et(issued_time_dt)} ({names})"
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_H2 = "font-size:1.35em;margin:24px 0 8px;font-weight:600"
_H3 = "font-size:1.1em;margin:16px 0 6px;font-weight:600;color:#3f4748"
_H4 = "font-size:0.95em;margin:10px 0 4px;font-weight:600;color:#5e6a6b"
_H5 = "font-size:0.85em;margin:8px 0 3px;font-weight:600;color:#7e8e8f"


def _parse_bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name, "")
    if val == "":
        return default
    return val.strip().lower() not in ("false", "0", "no")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--issued-time",
        required=False,
        default=None,
        help=(
            "Issued time of the forecast (format YYYY-MM-DDTHH). "
            "If omitted, defaults to the most recent NHC advisory hour "
            "(03/09/15/21 UTC) on or before the current UTC time."
        ),
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help=(
            "Generate the HTML, render it through the real Listmonk campaign "
            "template, and open it in the browser. Sends nothing; reuses one "
            "parked draft campaign rather than creating one per preview."
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "With --preview: render the FULL layout (both maps + all "
            "per-threshold strip charts) instead of the condensed email."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help=(
            "With --preview: skip the Listmonk round-trip and show the bare "
            "body. Faster, works offline, but without the email chrome."
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "With --preview: write the HTML here instead of a temp file. "
            "A stable path means the browser tab reloads onto the new render."
        ),
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="With --preview: write the file but do not open a browser.",
    )
    parser.add_argument(
        "--send-test",
        action="store_true",
        help=(
            f"Send the alert to the test list ({TEST_LIST_IDS}) and exit. "
            "Ignores TEST_EMAIL/DRY_RUN."
        ),
    )
    parser.add_argument(
        "--stage",
        default="dev",
        choices=["dev", "prod"],
        help=(
            "ocha-stratus DB/blob stage to read exposure data from "
            "(default: dev — where the storm pipeline currently writes)."
        ),
    )
    return parser.parse_args()


def _most_recent_advisory_time() -> datetime:
    """Most recent NHC advisory hour (03/09/15/21 UTC) on or before now."""
    now = datetime.now(UTC)
    for h in (21, 15, 9, 3):
        if now.hour >= h:
            return now.replace(hour=h, minute=0, second=0, microsecond=0, tzinfo=None)
    return (now - timedelta(days=1)).replace(
        hour=21, minute=0, second=0, microsecond=0, tzinfo=None
    )


TEST_EMAIL = _parse_bool_env("TEST_EMAIL", default=True)
DRY_RUN = _parse_bool_env("DRY_RUN", default=True)


def resolve_country_list_ids(client, iso3s: list[str]) -> list[int]:
    """Return listmonk list IDs for the given iso3s plus applicable aggregate lists.

    Fetches all lists tagged COUNTRY_LIST_TAG and builds:
    - iso3→list_id from iso3:* tags (one per country)
    - aggregate:all list ID (always included)
    - aggregate:lac list ID (included if any iso3 is in LAC_ISO3S)
    - aggregate:monitoring list ID(s) (always included) — intentional: monitoring
      subscribers (currently the DSci team) receive the full exposure alert too, as
      a redundant cross-check on top of the no-exposure monitoring email they get
      from the other branch.

    Raises RuntimeError if any per-country iso3 has no list.
    """
    all_lists = client.fetch_all_lists(tag=COUNTRY_LIST_TAG)
    iso3_to_list: dict[str, int] = {}
    aggregate_all_id: int | None = None
    aggregate_lac_id: int | None = None
    monitoring_ids: list[int] = []
    for lst in all_lists:
        for tag in lst.get("tags", []):
            if tag.startswith("iso3:"):
                iso3_to_list[tag[5:]] = lst["id"]
            elif tag == "aggregate:all":
                aggregate_all_id = lst["id"]
            elif tag == "aggregate:lac":
                aggregate_lac_id = lst["id"]
            elif tag == "aggregate:monitoring":
                monitoring_ids.append(lst["id"])

    missing = [iso3 for iso3 in iso3s if iso3 not in iso3_to_list]
    if missing:
        raise RuntimeError(
            f"No listmonk list found for iso3(s): {missing}. "
            f"Run pipelines/setup_country_lists.py first."
        )

    result = [iso3_to_list[iso3] for iso3 in iso3s]
    if aggregate_all_id is not None:
        result.append(aggregate_all_id)
    if aggregate_lac_id is not None and any(iso3 in LAC_ISO3S for iso3 in iso3s):
        result.append(aggregate_lac_id)
    result.extend(monitoring_ids)
    return result


def _fetch_monitoring_list_ids(client) -> list[int]:
    """Return IDs of all aggregate:monitoring lists."""
    all_lists = client.fetch_all_lists(tag=COUNTRY_LIST_TAG)
    return [
        lst["id"]
        for lst in all_lists
        for tag in lst.get("tags", [])
        if tag == "aggregate:monitoring"
    ]


def _landfall_panel_html(
    landfalls, adm1_gdf, cname, issued_time_dt: datetime
) -> str:
    """Forecast-landfall callout for one storm (both layouts).

    Computed from the NHC forecast track in our own DB — see src/landfall.py
    for why this is not pulled from PDC. Only centre crossings count; a country
    brushed by the wind field but never crossed shows exposure, not landfall.
    """
    if not landfalls:
        return ""
    rows: list[str] = []
    for lf in landfalls[:4]:
        country = cname(lf.iso3)
        if lf.already_inland:
            rows.append(
                f"<div style='margin:2px 0'><b style='color:#1f2324'>{country}"
                f"</b> — storm centre already inland at this advisory</div>"
            )
            continue
        near = nearest_adm1_name(lf.point, adm1_gdf, lf.iso3)
        near_part = f" near {near}" if near else ""
        lead_h = (lf.time - issued_time_dt).total_seconds() / 3600
        if lead_h < 1:
            lead = "imminent"
        elif lead_h < 48:
            lead = f"in ~{lead_h:.0f} h"
        else:
            lead = f"in ~{lead_h / 24:.1f} days"
        wind_part = ""
        if lf.wind_speed_kt and not math.isnan(lf.wind_speed_kt):
            cat = saffir_simpson(lf.wind_speed_kt)
            cat_part = f" ({cat})" if cat else ""
            wind_part = f" &middot; ~{lf.wind_speed_kt:.0f} kt{cat_part}"
        rows.append(
            f"<div style='margin:2px 0'>"
            f"<b style='color:#1f2324'>{country}</b>{near_part} &middot; "
            f"{_format_issued_et(lf.time)} ET ({lead}){wind_part}</div>"
        )
    return (
        "<div style='background:#e8effb;border-left:3px solid #1862d8;"
        "border-radius:0 6px 6px 0;padding:12px 16px;margin:12px 0 16px;"
        "font-size:0.92em;color:#3f4748;line-height:1.6'>"
        "<div style='font-size:0.8em;font-weight:600;letter-spacing:0.08em;"
        "text-transform:uppercase;color:#134ead;margin-bottom:4px'>"
        "Forecast landfall</div>"
        + "".join(rows)
        + "</div>"
    )


def generate_monitoring_html(
    engine, issued_time_dt: datetime, active_meta: list[dict]
) -> str:
    """Email body for advisories with active storms but no exposure.

    Includes the same per-storm maps as the normal alert (WSP 34 kt + buffers).
    No ToC, no country sections, no historical comparisons.
    """
    atcf_ids = [m["atcf_id"] for m in active_meta]

    logger.info("Fetching track geometries (monitoring)...")
    tracks_gdf = fetch_track_geo(engine, atcf_ids, issued_time_dt)
    logger.info("Fetching wind buffers (monitoring)...")
    buffers_gdf = fetch_buffers(engine, atcf_ids, issued_time_dt)
    logger.info("Fetching WSP fcastonly polygons (monitoring)...")
    wsp_gdf = fetch_wsp_fcastonly_polygons(
        engine, atcf_ids, issued_time_dt, wind_threshold_kt=34,
    )
    logger.info("Loading country boundaries (monitoring)...")
    background_gdf = load_background_countries()

    n = len(active_meta)
    intro = (
        f"<p style='font-family:sans-serif;color:#5e6a6b;"
        f"margin:0 0 24px;font-size:0.95em;line-height:1.5'>"
        f"{n} active storm{'s' if n != 1 else ''} at this advisory. "
        f"None {'are' if n != 1 else 'is'} currently forecast to affect "
        f"any monitored country.</p>"
    )

    sections: list[str] = [intro]
    for meta in active_meta:
        aid = meta["atcf_id"]
        storm_label = _storm_label(meta["name"], meta["season"])
        aid_tracks = tracks_gdf[tracks_gdf["atcf_id"] == aid]
        aid_buffers = buffers_gdf[buffers_gdf["atcf_id"] == aid]
        aid_wsp_poly = wsp_gdf[wsp_gdf["atcf_id"] == aid]

        parts: list[str] = [f"<h2 style='{_H2}'>{storm_label}</h2>"]
        buf_m = track_plot_buffers(
            aid_tracks, aid_buffers, background_gdf, storm_name=storm_label,
        )
        if buf_m:
            parts.append(f"<h3 style='{_H3}'>Deterministic forecast</h3>{buf_m}")
        wsp_m = track_plot_wsp(
            aid_tracks, aid_buffers, aid_wsp_poly, background_gdf,
            wind_threshold_kt=34, storm_name=storm_label,
        )
        if wsp_m:
            parts.append(
                f"<h3 style='{_H3}'>Probabilistic forecast</h3>{wsp_m}"
            )
        sections.append("".join(parts))

    return "".join(sections)


def generate_alert_html(
    engine, issued_time_dt: datetime, full: bool = False
) -> tuple[str, list[str]] | None:
    """Run the full pipeline and return (html_body, iso3s, storm_names).

    Two layouts share one data pass:

    - ``full=False`` (the email, and the default): letter, summary table, ONE
      map per storm (track + swath edges + admin-1 exposure choropleth), and
      any final-update notices. No per-country strip charts, no probabilistic
      map — the numbers live in the table and the attached workbook, the full
      charts live online. This is the condensed layout the alert ships with.
    - ``full=True``: the everything version — deterministic + probabilistic
      maps and the per-country, per-threshold strip charts. Rendered to the
      online example pages, not emailed.

    Returns None if there are no countries with any forecasted exposure and no
    storm-country pairs eligible for a final update notice.
    """
    issued_time = issued_time_dt.strftime("%Y-%m-%dT%H")
    logger.info("Fetching forecast exposure...")
    fcast_df = fetch_fcast_exposure(engine, issued_time_dt)

    all_atcf_ids = fcast_df["atcf_id"].unique().tolist()

    # Fetch previous advisory pairs first so we can extend the WSP seed to include
    # storms that have WSP exposure but no track fcastonly exposure this advisory.
    logger.info("Fetching previous advisory exposure (for final update detection)...")
    prev_any_rows = fetch_prev_any_pairs(engine, issued_time_dt)
    prev_any_pairs = {(r["atcf_id"], r["iso3"]) for r in prev_any_rows}
    prev_atcf_ids = sorted({r["atcf_id"] for r in prev_any_rows})

    all_wsp_seed_ids = sorted(set(all_atcf_ids) | set(prev_atcf_ids))
    logger.info("Fetching WSP fcastonly exposure (all wind speeds)...")
    wsp_exp_df = fetch_wsp_fcastonly_exposure(engine, all_wsp_seed_ids, issued_time_dt)

    # Trigger: any (atcf_id, iso3) pair with non-zero exposure at any wind speed
    # from either WSP fcastonly or track fcastonly.
    current_any_pairs = (
        {(r.atcf_id, r.iso3) for r in fcast_df.itertuples()}
        | {(r.atcf_id, r.iso3) for r in wsp_exp_df.itertuples() if r.pop_exposed > 0}
    )
    if current_any_pairs:
        atcf_ids = sorted({aid for aid, _ in current_any_pairs})
        iso3s = sorted({iso3 for _, iso3 in current_any_pairs})
    else:
        atcf_ids, iso3s = [], []

    # Final update pairs: had any exposure in previous advisory, have none now.
    final_update_pairs: set[tuple[str, str]] = prev_any_pairs - current_any_pairs
    final_update_meta: dict[tuple[str, str], tuple] = {
        (r["atcf_id"], r["iso3"]): (r["name"], r["season"])
        for r in prev_any_rows
        if (r["atcf_id"], r["iso3"]) in final_update_pairs
    }

    if not current_any_pairs and not final_update_pairs:
        return None


    # Extend fetch lists to cover final-update storms/countries.
    extra_atcf_ids = sorted({aid for aid, _ in final_update_pairs} - set(atcf_ids))
    all_fetch_atcf_ids = atcf_ids + extra_atcf_ids

    logger.info(
        f"Active storms: {atcf_ids}, affected countries: {iso3s}"
        + (f", final-update candidates: {extra_atcf_ids}" if extra_atcf_ids else "")
    )

    logger.info("Fetching current observed exposure...")
    obsv_df = fetch_current_obsv_exposure(engine, all_fetch_atcf_ids, issued_time_dt)

    # Filter final_update_pairs: only keep pairs with observed exposure (cumulative).
    obsv_pairs = {
        (r.atcf_id, r.iso3) for r in obsv_df.itertuples() if r.pop_exposed > 0
    }
    final_update_pairs = {pair for pair in final_update_pairs if pair in obsv_pairs}

    # Recompute render lists after observed filter.
    extra_iso3s = sorted({iso3 for _, iso3 in final_update_pairs} - set(iso3s))
    all_render_iso3s = iso3s + extra_iso3s
    all_render_atcf_ids = sorted(
        set(atcf_ids) | {aid for aid, _ in final_update_pairs}
    )

    logger.info("Fetching historical observed exposure...")
    hist_df = fetch_historical_obsv_exposure(
        engine, all_render_iso3s, exclude_atcf_ids=all_render_atcf_ids
    )
    hist_df = hist_df[hist_df["season"] >= 2002].reset_index(drop=True)

    iso3_to_total_pop = fetch_admin_population(engine, all_render_iso3s)

    all_prior_pairs = fetch_all_prior_country_pairs(engine, all_render_atcf_ids, issued_time_dt)
    obsv_with_exposure = {
        (r.atcf_id, r.iso3) for r in obsv_df.itertuples() if r.pop_exposed > 0
    }
    already_passed_pairs: dict[tuple[str, str], datetime] = {
        k: v for k, v in all_prior_pairs.items()
        if k not in current_any_pairs
        and k not in final_update_pairs
        and k in obsv_with_exposure
    }

    logger.info("Fetching GDACS current exposure...")
    gdacs_cur_df = fetch_gdacs_current_exposure(engine, all_render_atcf_ids, issued_time_dt)

    logger.info("Fetching GDACS historical exposure...")
    gdacs_hist_df = fetch_gdacs_historical_exposure(
        engine, all_render_iso3s, exclude_atcf_ids=all_render_atcf_ids
    )
    gdacs_hist_df = gdacs_hist_df[gdacs_hist_df["season"] >= 2002].reset_index(drop=True)

    logger.info("Fetching ADAM current exposure...")
    adam_cur_df = fetch_adam_current_exposure(engine, all_render_atcf_ids, issued_time_dt)

    logger.info("Fetching ADAM historical exposure...")
    adam_hist_df = fetch_adam_historical_exposure(
        engine, all_render_iso3s, exclude_atcf_ids=all_render_atcf_ids
    )
    adam_hist_df = adam_hist_df[adam_hist_df["season"] >= 2002].reset_index(drop=True)

    logger.info("Fetching track geometries...")
    tracks_gdf = fetch_track_geo(engine, all_fetch_atcf_ids, issued_time_dt)

    logger.info("Fetching wind buffers...")
    buffers_gdf = fetch_buffers(engine, all_fetch_atcf_ids, issued_time_dt)

    logger.info("Fetching WSP fcastonly polygons (34 kt) for map...")
    wsp_gdf = fetch_wsp_fcastonly_polygons(
        engine, all_fetch_atcf_ids, issued_time_dt, wind_threshold_kt=34,
    )

    logger.info("Loading country boundaries...")
    background_gdf = load_background_countries()
    _all_name_iso3s = sorted(
        set(all_render_iso3s) | {iso3 for _, iso3 in already_passed_pairs}
    )
    adm1_gdf = load_adm1_boundaries(_all_name_iso3s)
    def _mode_name(x):
        # value_counts() drops NaN, so an all-NaN group yields an empty Series;
        # return None then so _cname falls back to the iso3 code (no IndexError).
        vc = x.value_counts()
        return vc.index[0] if len(vc) else None

    iso3_to_name: dict[str, str] = {
        k: v
        for k, v in adm1_gdf.groupby("iso_3")["adm0_name"]
        .agg(_mode_name).to_dict().items()
        if v is not None
    }

    def _cname(iso3: str) -> str:
        return iso3_to_name.get(iso3, iso3)

    # Admin-1 exposure feeds the combined email map — the same consolidated
    # 34 kt MAX per FieldMaps unit as the attached workbook's adm1 tab, so the
    # map and the spreadsheet can never disagree. Skipped in full mode, which
    # renders the original map pair instead.
    if not full:
        import pandas as _pd

        logger.info("Fetching admin-1 exposure for the email map...")
        fcast_adm1_df = fetch_fcast_exposure_adm1(engine, issued_time_dt)
        obsv_adm1_df = fetch_current_obsv_exposure_adm1(
            engine, all_fetch_atcf_ids, issued_time_dt)
        gdacs_adm1_df = fetch_gdacs_current_exposure_adm1(
            engine, all_fetch_atcf_ids, issued_time_dt)
        adam_adm1_df = fetch_adam_current_exposure_adm1(
            engine, all_fetch_atcf_ids, issued_time_dt)
        # GDACS units with no FieldMaps match can't be drawn (no geometry key).
        gdacs_adm1_df = gdacs_adm1_df[gdacs_adm1_df["fm_pcode"].notna()].copy()
        fm_name_by_pcode = fetch_fm_names(engine, all_render_iso3s)

    logger.info("Generating plots...")

    sections: list[str] = []

    def _marks(df, iso3, wsp, color, suffix="", short=False):
        sub = df[(df["iso3"] == iso3) & (df["wind_speed_kt"] == wsp)]
        return [
            StormMark(
                value=int(row["pop_exposed"]),
                label=_storm_label(row["name"], row["season"], suffix),
                color=color,
                short=short,
            )
            for _, row in sub.iterrows()
            if row["pop_exposed"] > 0
        ]

    def _filter_historical(
        hist_marks: list[StormMark],
        x_max: float,
        current_values: list[float] | None = None,
    ) -> list[StormMark]:
        """Keep the highest-value historical storms; drop ones too close to a
        bigger neighbour or to a current/forecast mark, and drop storms below
        a minimum absolute value (relative to x_max)."""
        if not hist_marks or x_max <= 0:
            return hist_marks
        sorted_marks = sorted(hist_marks, key=lambda m: m.value, reverse=True)
        min_gap = x_max * 0.025
        min_value = x_max * 0.005
        blocked = list(current_values or [])
        kept: list[StormMark] = []
        for m in sorted_marks:
            if m.value < min_value:
                continue
            if any(abs(m.value - v) < min_gap for v in blocked):
                continue
            if all(abs(m.value - k.value) >= min_gap for k in kept):
                kept.append(m)
        return kept

    def _obsv_for(df, atcf_id: str, iso3: str, wsp: int) -> int:
        m = df[
            (df["atcf_id"] == atcf_id)
            & (df["iso3"] == iso3)
            & (df["wind_speed_kt"] == wsp)
        ]
        return int(m["pop_exposed"].sum()) if not m.empty else 0

    def _max_for_wsp(wsp: int) -> float:
        sources = [
            fcast_df, obsv_df, hist_df,
            gdacs_cur_df, gdacs_hist_df,
            adam_cur_df, adam_hist_df,
        ]
        candidates = [0.0]
        for src in sources:
            sub = src[src["wind_speed_kt"] == wsp]
            if not sub.empty:
                candidates.append(float(sub["pop_exposed"].max()))
        # Forecast total (fcast + obsv) — can exceed either individually.
        f = fcast_df[fcast_df["wind_speed_kt"] == wsp]
        if not f.empty:
            for _, row in f.iterrows():
                total = float(row["pop_exposed"]) + _obsv_for(
                    obsv_df, row["atcf_id"], row["iso3"], wsp,
                )
                candidates.append(total)
        # WSP fcastonly PDF tail = obsv + cumulative fcastonly pop across bands.
        w = wsp_exp_df[wsp_exp_df["wind_threshold_kt"] == wsp]
        if not w.empty:
            for (atcf_id, iso3), grp in w.groupby(["atcf_id", "iso3"]):
                obsv = _obsv_for(obsv_df, atcf_id, iso3, wsp)
                candidates.append(obsv + float(grp["pop_exposed"].sum()))
        return max(candidates)

    # Storm metadata for section headers.
    # Priority: fcast_df (most current) > prev_any_rows (covers WSP-only storms
    # that have no track fcast exposure) > final_update_meta.
    storm_meta: dict[str, tuple] = {}
    for r in prev_any_rows:
        if r["atcf_id"] not in storm_meta:
            storm_meta[r["atcf_id"]] = (r["name"], r["season"])
    for _, row in fcast_df.drop_duplicates("atcf_id").iterrows():
        storm_meta[row["atcf_id"]] = (row["name"], row["season"])
    for (aid, _), (nm, ssn) in final_update_meta.items():
        if aid not in storm_meta:
            storm_meta[aid] = (nm, ssn)

    # Storm-to-country mapping for rendering.
    storm_to_iso3s: dict[str, set[str]] = {}
    for aid, iso3 in current_any_pairs:
        storm_to_iso3s.setdefault(aid, set()).add(iso3)
    for aid, iso3 in final_update_pairs:
        storm_to_iso3s.setdefault(aid, set()).add(iso3)

    wind_speeds_in_order = (64, 50, 34)
    x_max_per_wsp = {wsp: _max_for_wsp(wsp) for wsp in wind_speeds_in_order}

    def _storm_exposure_score(aid: str) -> float:
        sub_fcast = fcast_df[fcast_df["atcf_id"] == aid]
        sub_obsv = obsv_df[obsv_df["atcf_id"] == aid]
        candidates: list[float] = [0.0]
        if not sub_fcast.empty:
            candidates.append(float(sub_fcast["pop_exposed"].max()))
        if not sub_obsv.empty:
            candidates.append(float(sub_obsv["pop_exposed"].max()))
        return max(candidates)

    def _country_exposure_score(aid: str, iso3: str) -> float:
        sub_fcast = fcast_df[(fcast_df["atcf_id"] == aid) & (fcast_df["iso3"] == iso3)]
        sub_obsv = obsv_df[(obsv_df["atcf_id"] == aid) & (obsv_df["iso3"] == iso3)]
        candidates: list[float] = [0.0]
        if not sub_fcast.empty:
            candidates.append(float(sub_fcast["pop_exposed"].max()))
        if not sub_obsv.empty:
            candidates.append(float(sub_obsv["pop_exposed"].max()))
        return max(candidates)

    def _country_rp_score(aid: str, iso3: str) -> float:
        """Max RP across all wind speeds for this country/storm (higher = rarer)."""
        best = 0.0
        for _tw in (34, 50, 64):
            _tr = fcast_df[
                (fcast_df["atcf_id"] == aid)
                & (fcast_df["iso3"] == iso3)
                & (fcast_df["wind_speed_kt"] == _tw)
            ]
            _fv = int(_tr["pop_exposed"].iloc[0]) if not _tr.empty else 0
            _ov = _obsv_for(obsv_df, aid, iso3, _tw)
            _tot = _fv + _ov
            if _tot > 0:
                rp = _rp_numeric(float(_tot), iso3, _tw)
                if rp is not None and rp > best:
                    best = rp
        return best

    n_seasons = issued_time_dt.year - 2002 + 1

    def _fmt_pop_toc(x: float) -> str:
        if x >= 1_000_000:
            return f"{x / 1_000_000:.1f}M"
        if x >= 1_000:
            return f"{x / 1_000:.0f}K"
        return str(int(x))

    def _rp_numeric(forecast_val: float, iso3: str, wsp: int) -> float | None:
        if forecast_val <= 0:
            return None
        hist_vals = hist_df[
            (hist_df["iso3"] == iso3) & (hist_df["wind_speed_kt"] == wsp)
        ]["pop_exposed"].tolist()
        exceedances = sum(1 for v in hist_vals if v >= forecast_val)
        return (n_seasons + 1) / (exceedances + 1)

    def _rp_color(rp: float | None) -> str:
        if rp is None:
            return ""
        # Colour by the displayed (rounded) value so e.g. 4.6 → "5-year" gets the
        # same colour as any other 5-year RP, matching the f"{rp:.0f}" label.
        rp = round(rp)
        if rp <= 3:
            return ""
        if rp > 10:
            return "#e7b5af"
        if rp > 5:
            return "#e5bc7f"
        return "#f6e9d4"

    toc_storms: list[dict] = []

    _ordered_aids = sorted(
        storm_to_iso3s.keys(), key=lambda a: -_storm_exposure_score(a)
    )
    for aid in _ordered_aids:
        s_name, s_season = storm_meta.get(aid, (None, None))
        storm_h2_label = _storm_label(s_name, s_season)

        # Pre-filter DataFrames to this storm for per-storm mark computation.
        aid_gdacs_cur = (
            gdacs_cur_df[gdacs_cur_df["atcf_id"] == aid]
            if "atcf_id" in gdacs_cur_df.columns else gdacs_cur_df
        )
        aid_adam_cur = (
            adam_cur_df[adam_cur_df["atcf_id"] == aid]
            if "atcf_id" in adam_cur_df.columns else adam_cur_df
        )
        tr_storm = fcast_df[fcast_df["atcf_id"] == aid]
        name_aid = tr_storm["name"].iloc[0] if not tr_storm.empty else s_name

        # Per-storm maps (WSP + buffers filtered to this storm only).
        aid_tracks = tracks_gdf[tracks_gdf["atcf_id"] == aid]
        aid_buffers = buffers_gdf[buffers_gdf["atcf_id"] == aid]
        aid_wsp_poly = wsp_gdf[wsp_gdf["atcf_id"] == aid]
        aid_adm1 = adm1_gdf[adm1_gdf["iso_3"].isin(storm_to_iso3s[aid])]

        # Forecast landfall: first centre crossing per affected country.
        _geom_by_iso3: dict[str, object] = {}
        if not aid_adm1.empty:
            for _iso3_g, _grp in aid_adm1.groupby("iso_3"):
                _geom_by_iso3[_iso3_g] = _grp.geometry.union_all()
        _lf_missing = sorted(set(storm_to_iso3s[aid]) - set(_geom_by_iso3))
        if _lf_missing:
            _lf_adm0 = load_adm0_boundaries(_lf_missing)
            for _r0 in _lf_adm0.itertuples():
                _geom_by_iso3.setdefault(_r0.iso_3, _r0.geometry)
        landfall_html = _landfall_panel_html(
            compute_landfalls(aid_tracks, _geom_by_iso3),
            adm1_gdf, _cname, issued_time_dt,
        )

        storm_map_parts: list[str] = []
        if full:
            buf_m = track_plot_buffers(
                aid_tracks, aid_buffers, background_gdf,
                adm1_gdf=aid_adm1, storm_name=storm_h2_label,
            )
            if buf_m:
                storm_map_parts.append(
                    f"<h3 style='{_H3}'>Deterministic forecast</h3>{buf_m}"
                )
            wsp_m = track_plot_wsp(
                aid_tracks, aid_buffers, aid_wsp_poly, background_gdf,
                wind_threshold_kt=34, adm1_gdf=aid_adm1,
                storm_name=storm_h2_label,
            )
            if wsp_m:
                storm_map_parts.append(
                    f"<h3 style='{_H3}'>Probabilistic forecast</h3>{wsp_m}"
                )
        # Email mode: the single combined map is built AFTER the country loop —
        # it needs the consolidated per-country 34 kt totals collected there.

        toc_countries: list[dict] = []
        country_sections: list[str] = []
        adm0_exp_34: dict[str, int] = {}
        for iso3 in sorted(storm_to_iso3s[aid], key=lambda c: -_country_rp_score(aid, c)):
            # Final update notice for this (storm, country) pair.
            notice_html = ""
            if (aid, iso3) in final_update_pairs:
                storm_lbl = (
                    name_aid.strip().title()
                    if isinstance(name_aid, str) and name_aid
                    else aid
                )
                _cn = _cname(iso3)
                notice_html = (
                    f"<p style='background:#fbf4ea;border-left:4px solid #d48f2a;"
                    f"padding:10px 14px;margin:12px 0;font-size:0.95em'>"
                    f"This is the last update for <strong>{storm_lbl}</strong> in "
                    f"<strong>{_cn}</strong> as there is no further forecasted "
                    f"exposure. Figures below and attached data indicate purely "
                    f"observed exposure and will not change, unless the track of the "
                    f"storm changes significantly and returns towards {_cn} again. "
                    f"In this case another update will be issued for "
                    f"{storm_lbl} in {_cn}.</p>"
                )

            # Only render wind speeds that have current data for this (storm, country).
            active_wsps = [
                wsp for wsp in wind_speeds_in_order
                if (
                    (_wsp_expected_pop(wsp_exp_df, aid, iso3, wsp) or 0) > 0
                    or not fcast_df[
                        (fcast_df["atcf_id"] == aid)
                        & (fcast_df["iso3"] == iso3)
                        & (fcast_df["wind_speed_kt"] == wsp)
                    ].empty
                    or _obsv_for(obsv_df, aid, iso3, wsp) > 0
                )
            ]
            if not active_wsps:
                continue

            _toc_wsps: list[dict] = []
            for _tw in (34, 50, 64):
                _tr = fcast_df[
                    (fcast_df["atcf_id"] == aid)
                    & (fcast_df["iso3"] == iso3)
                    & (fcast_df["wind_speed_kt"] == _tw)
                ]
                _fv = int(_tr["pop_exposed"].iloc[0]) if not _tr.empty else 0
                _ov = _obsv_for(obsv_df, aid, iso3, _tw)
                _our = _fv + _ov
                _gdacs_r = aid_gdacs_cur[
                    (aid_gdacs_cur["iso3"] == iso3)
                    & (aid_gdacs_cur["wind_speed_kt"] == _tw)
                ]
                _gdacs_v = int(_gdacs_r["pop_exposed"].iloc[0]) if not _gdacs_r.empty else 0
                _adam_r = aid_adam_cur[
                    (aid_adam_cur["iso3"] == iso3)
                    & (aid_adam_cur["wind_speed_kt"] == _tw)
                ]
                _adam_v = int(_adam_r["pop_exposed"].iloc[0]) if not _adam_r.empty else 0
                _toc_active = {
                    k: v for k, v in
                    {"our": _our, "ADAM": _adam_v, "GDACS": _gdacs_v}.items()
                    if v > 0
                }
                # Combine sources by MAX, not mean: alerts bias to action — the
                # highest credible estimate across CHD/ADAM/GDACS is the
                # operationally relevant figure, even when one source is conservative.
                _tot = max(_toc_active.values()) if _toc_active else 0
                if _tw == 34:
                    # Feeds the combined email map's national-fallback shading.
                    adm0_exp_34[iso3] = _tot
                if _tot > 0:
                    _tp = iso3_to_total_pop.get(iso3, 0)
                    _toc_wsps.append({
                        "wsp": _tw,
                        "total": _tot,
                        "rp": _rp_numeric(float(_our), iso3, _tw),
                        "pct": _tot / _tp * 100 if _tp > 0 else None,
                    })
            if _toc_wsps:
                # Find most similar historical storms by relative-difference score
                # across all three wind speeds (lower score = more similar).
                _curr_vec = {34: 0, 50: 0, 64: 0}
                for _tw_d in _toc_wsps:
                    _curr_vec[_tw_d["wsp"]] = _tw_d["total"]

                _hist_storms_by_id: dict[str, dict] = {}
                for _, _hr in hist_df[hist_df["iso3"] == iso3].iterrows():
                    _aid_h = _hr["atcf_id"]
                    if _aid_h not in _hist_storms_by_id:
                        _hist_storms_by_id[_aid_h] = {
                            "name": _hr["name"], "season": _hr["season"],
                            34: 0, 50: 0, 64: 0,
                        }
                    _hist_storms_by_id[_aid_h][int(_hr["wind_speed_kt"])] = int(_hr["pop_exposed"])

                _sim_scores: list[tuple[float, str]] = []
                for _hd in _hist_storms_by_id.values():
                    _score = sum(
                        abs(_curr_vec[_ws] - _hd[_ws]) / max(_curr_vec[_ws], _hd[_ws], 1)
                        for _ws in (34, 50, 64)
                    )
                    _sim_scores.append((_score, _storm_label(_hd["name"], _hd["season"])))
                _sim_scores.sort()
                _similar = [_lbl for _, _lbl in _sim_scores[:3]]

                toc_countries.append({
                    "name": _cname(iso3),
                    "is_final": (aid, iso3) in final_update_pairs,
                    "wsps": _toc_wsps,
                    "similar": _similar,
                })

            # Condensed email: one chart per country — the source comparison
            # at the highest threshold with an actual source estimate. A
            # threshold can be "active" on WSP probability alone with every
            # source at zero, and that chart is an empty strip: for Haiti
            # under Melissa it would show 64 kt with nothing in it instead of
            # the 5.4M-at-34 kt story. All thresholds render in full mode.
            if full:
                wsps_to_render = active_wsps
            else:
                _est_wsps = [w["wsp"] for w in _toc_wsps]
                wsps_to_render = (
                    [max(_est_wsps)] if _est_wsps else active_wsps[:1]
                )

            combined_blocks: list[str] = []
            _total_pop = iso3_to_total_pop.get(iso3, 0)

            for wsp in wsps_to_render:
                wsp_color = wind_speed_color(wsp)
                obsv_floor = _obsv_for(obsv_df, aid, iso3, wsp)

                # Our estimate: deterministic track fcastonly + cumulative observed.
                tr_row = fcast_df[
                    (fcast_df["atcf_id"] == aid)
                    & (fcast_df["iso3"] == iso3)
                    & (fcast_df["wind_speed_kt"] == wsp)
                ]
                fcast_val = int(tr_row["pop_exposed"].iloc[0]) if not tr_row.empty else 0
                our_val = fcast_val + obsv_floor

                # GDACS and ADAM current estimates for this country/wind speed.
                gdacs_row = aid_gdacs_cur[
                    (aid_gdacs_cur["iso3"] == iso3)
                    & (aid_gdacs_cur["wind_speed_kt"] == wsp)
                ]
                gdacs_val = int(gdacs_row["pop_exposed"].iloc[0]) if not gdacs_row.empty else 0

                adam_row = aid_adam_cur[
                    (aid_adam_cur["iso3"] == iso3)
                    & (aid_adam_cur["wind_speed_kt"] == wsp)
                ]
                adam_val = int(adam_row["pop_exposed"].iloc[0]) if not adam_row.empty else 0

                active_sources = {
                    k: v for k, v in
                    {"our": our_val, "ADAM": adam_val, "GDACS": gdacs_val}.items()
                    if v > 0
                }

                # WSP data for this country/storm/wsp — needed for x_max and PDF.
                wsp_sub = wsp_exp_df[
                    (wsp_exp_df["atcf_id"] == aid)
                    & (wsp_exp_df["iso3"] == iso3)
                    & (wsp_exp_df["wind_threshold_kt"] == wsp)
                ]

                # x_max = min(total_pop, 2 × wsp_extent), extended if any mark exceeds it.
                # wsp_extent = right edge of the PDF polygon (obsv_floor + sum of all bands),
                # which is the "maximum value from the WSP distribution" as seen on the chart.
                _wsp_extent = obsv_floor + float(wsp_sub["pop_exposed"].sum()) if not wsp_sub.empty else 0.0
                _base = _wsp_extent if _wsp_extent > 0 else (
                    max(active_sources.values()) if active_sources else x_max_per_wsp[wsp]
                )
                _cap = min(_total_pop, 2 * _base) if _total_pop > 0 else 2 * _base
                _max_active = max(active_sources.values()) if active_sources else 0
                _chart_xmax = max(_cap, _max_active)
                # show "total pop." tick only when total_pop is visible on this chart
                _chart_total_pop = (
                    _total_pop if (_total_pop > 0 and _total_pop <= _chart_xmax) else None
                )

                hist_marks = _filter_historical(
                    _marks(hist_df, iso3, wsp, _HIST_COLOR, short=True),
                    _chart_xmax,
                    current_values=list(active_sources.values()),
                )

                if active_sources:
                    # The consolidated headline (MAX across sources — bias to
                    # action, see _tot in the ToC loop) lives in the summary
                    # table; on the chart it is by definition the longest bar,
                    # so it is not drawn a second time.
                    _is_final = (aid, iso3) in final_update_pairs
                    source_ticks = [
                        StormMark(value=v, label=_SRC_LABELS[k], color=wsp_color, short=False)
                        for k, v in active_sources.items()
                    ]
                    obs_ticks = (
                        [StormMark(
                            value=obsv_floor,
                            label="observed so far",
                            color=wsp_color,
                            bold=True,
                        )]
                        if obsv_floor > 0 and not _is_final else []
                    )
                    combined_marks = hist_marks + obs_ticks + source_ticks
                else:
                    combined_marks = hist_marks

                pdf = None
                if not wsp_sub.empty:
                    pdf = WspPdf(
                        bands=[
                            (int(r["percentage"]), int(r["pop_exposed"]))
                            for _, r in wsp_sub.iterrows()
                        ],
                        x_offset=float(obsv_floor),
                        color=wsp_color,
                    )

                # The KDE gets the FULL historical sample — the tick marks are
                # thinned for label spacing, and a thinned sample would bias
                # the curve.
                _hist_vals_all = hist_df[
                    (hist_df["iso3"] == iso3)
                    & (hist_df["wind_speed_kt"] == wsp)
                ]["pop_exposed"].tolist()
                combined_img = country_strip_chart(
                    iso3, wsp, combined_marks,
                    x_max=_chart_xmax,
                    pdf=pdf,
                    total_pop=_chart_total_pop,
                    hist_values=_hist_vals_all,
                )
                # No per-chart RP note: the return period lives in exactly two
                # places — the summary table and the country heading pill.
                _rp_html = ""
                # No per-threshold heading: the in-chart chip names both the
                # quantity and the threshold.
                combined_blocks.append(f"{combined_img}{_rp_html}")

            # The country's highest RP rides its heading as a colour pill —
            # the chart may feature a threshold whose own RP is unremarkable
            # while a lower threshold is the once-a-decade story.
            _c_best_rp = max(
                (w["rp"] for w in _toc_wsps if w["rp"]), default=None
            )
            if _c_best_rp is not None:
                # Always present, so the reader learns ONE place to look for
                # it; colour only when the value is notable.
                _pc = _rp_color(_c_best_rp) or "#ebeff0"
                _c_lbl = (
                    "&lt;1-year" if _c_best_rp < 1 else f"{_c_best_rp:.0f}-year"
                )
                _c_pill = (
                    f" <span style='display:inline-block;padding:1px 10px;"
                    f"border-radius:999px;background:{_pc};color:#1f2324;"
                    f"font-size:0.72em;font-weight:600;vertical-align:2px'>"
                    f"{_c_lbl} RP</span>"
                )
            else:
                _c_pill = ""
            country_sections.append(
                f"<h3 style='{_H3}'>{_cname(iso3)}{_c_pill}</h3>"
                + notice_html
                + "".join(combined_blocks)
            )

        if toc_countries:
            toc_storms.append(
                {"label": storm_h2_label, "aid": aid, "countries": toc_countries}
            )

        if not full:
            _adm1_rows = _build_adm1_rows(
                aid, storm_to_iso3s[aid], final_update_pairs, iso3_to_name,
                fm_name_by_pcode, fcast_adm1_df, obsv_adm1_df,
                gdacs_adm1_df, adam_adm1_df,
            )
            adm1_exp = _pd.DataFrame(
                [
                    {"iso3": r["iso3"], "fm_pcode": r["admin_pcode"],
                     "pop_exposed": r["pop_exposed"]}
                    for r in _adm1_rows
                    if r["wind_speed_kt"] == 34 and r["pop_exposed"] > 0
                ],
                columns=["iso3", "fm_pcode", "pop_exposed"],
            )
            # adm0 geometry only for countries that need the national fallback
            # (exposure but no shaded adm1 unit) — loading it for every country
            # would be wasted blob reads.
            _fallback_iso3s = sorted(
                {k for k, v in adm0_exp_34.items() if v > 0}
                - set(adm1_exp["iso3"])
            )
            _adm0_gdf = (
                load_adm0_boundaries(_fallback_iso3s) if _fallback_iso3s
                else None
            )
            exp_m = track_plot_exposure(
                aid_tracks, aid_buffers, background_gdf, aid_adm1,
                adm1_exp, adm0_exp_34, adm0_gdf=_adm0_gdf,
                storm_name=storm_h2_label,
            )
            if exp_m:
                storm_map_parts.append(exp_m)

        howto_html = ""
        if country_sections:
            howto_html = (
                "<p style='font-size:0.8em;color:#7e8e8f;line-height:1.7;"
                "margin:18px 0 2px;padding:10px 14px;background:#fafbfb;"
                "border:1px solid #ebeff0;border-radius:6px'>"
                "<b style='color:#5e6a6b'>Reading the charts below:</b> "
                "everything sits on one axis, population exposed. "
                "<b style='color:#5e6a6b'>Dots</b> are the current estimates "
                "by source (labelled beneath the axis) &middot; "
                "<b style='color:#5e6a6b'>vertical ticks</b> are past storms "
                "since 2002, and the <b style='color:#5e6a6b'>grey curve</b> "
                "their distribution &middot; the <b style='color:#5e6a6b'>"
                "coloured curve</b> is the forecast probabilistic "
                "distribution of exposure from NHC's wind-speed "
                "probabilities — its spike at the low end is the chance that "
                "little or no further exposure happens &middot; a "
                "<b style='color:#5e6a6b'>dashed line</b> marks exposure "
                "already observed.</p>"
            )
        if storm_map_parts or country_sections:
            sections.append(
                f"<h2 id='storm-{aid}' style='{_H2}'>{storm_h2_label}</h2>"
                + landfall_html
                + "".join(storm_map_parts)
                + howto_html
                + "".join(country_sections)
            )

    # Summary table, styled to the HDX tokens: horizontal hairlines only (no
    # cell borders), uppercase muted header, and the return period as a colour
    # pill instead of tinting whole cells — the old full-cell washes made the
    # table read like a heatmap of everything.
    _TD = "padding:9px 12px;vertical-align:top;border-bottom:1px solid #ebeff0"
    _TH = (
        "padding:10px 12px;text-align:left;font-weight:600;font-size:0.76em;"
        "color:#5e6a6b;text-transform:uppercase;letter-spacing:0.05em;"
        "border-bottom:2px solid #d8e0e1;white-space:nowrap"
    )

    def _rp_pill(rp: float | None) -> str:
        """Return period as a rounded pill; plain muted text when unremarkable.

        An RP under a year (most storms exceed this value) is real but
        "0-year" is not a meaningful display of it.
        """
        if rp is None:
            return "<span style='color:#9db1b3'>&mdash;</span>"
        label = "&lt;1-year" if rp < 1 else f"{rp:.0f}-year"
        color = _rp_color(rp)
        if not color:
            return f"<span style='color:#5e6a6b'>{label}</span>"
        return (
            f"<span style='display:inline-block;padding:2px 10px;"
            f"border-radius:999px;background:{color};color:#1f2324;"
            f"font-size:0.9em;font-weight:600;white-space:nowrap'>"
            f"{label}</span>"
        )

    tbl_rows: list[str] = []
    for _st in toc_storms:
        _st_total_rows = sum(len(_c["wsps"]) for _c in _st["countries"])
        _st_first = True
        for _c in _st["countries"]:
            _c_rows = len(_c["wsps"])
            _c_name = _c["name"]
            if _c["is_final"]:
                _c_name += (
                    " <em style='font-weight:normal;color:#9db1b3;"
                    "font-size:0.85em'>(final)</em>"
                )
            _c_first = True
            for _w in _c["wsps"]:
                _row = "<tr>"
                if _st_first:
                    _st_link = (
                        f"<a href='#storm-{_st['aid']}' "
                        f"style='color:#1f2324;text-decoration:underline'>"
                        f"{_st['label']}</a>"
                    )
                    _row += (
                        f"<td rowspan='{_st_total_rows}' style='{_TD};"
                        f"font-weight:700'>{_st_link}</td>"
                    )
                    _st_first = False
                if _c_first:
                    _row += (
                        f"<td rowspan='{_c_rows}' style='{_TD};"
                        f"font-weight:600;color:#1f2324'>{_c_name}</td>"
                    )
                    _sim_html = "<br>".join(_c.get("similar", [])) or "—"
                    _row += (
                        f"<td rowspan='{_c_rows}' style='{_TD};font-size:0.82em;"
                        f"color:#7e8e8f'>{_sim_html}</td>"
                    )
                    _c_first = False
                _pct_part = ""
                if _w.get("pct") is not None:
                    _pct_int = min(int(round(_w["pct"])), 100)
                    _pct_part = (
                        f" <span style='color:#9db1b3;font-size:0.85em'>"
                        f"({_pct_int}%)</span>"
                    )
                _row += (
                    f"<td style='{_TD};text-align:center;color:#5e6a6b;"
                    f"white-space:nowrap'>{_w['wsp']} kt</td>"
                    f"<td style='{_TD};text-align:right;white-space:nowrap'>"
                    f"<b style='color:#1f2324'>{_fmt_pop_toc(_w['total'])}</b>"
                    f"{_pct_part}</td>"
                    f"<td style='{_TD}'>{_rp_pill(_w['rp'])}</td>"
                    f"</tr>"
                )
                tbl_rows.append(_row)

    toc_html = (
        f"<table style='width:100%;border-collapse:collapse;"
        f"margin:0 0 10px;font-size:0.9em;background:#fff'>"
        f"<thead><tr>"
        f"<th style='{_TH}'>Storm</th>"
        f"<th style='{_TH}'>Country</th>"
        f"<th style='{_TH}'>Similar storms</th>"
        f"<th style='{_TH}'>Wind</th>"
        f"<th style='{_TH};text-align:right'>Exposure [% pop.]</th>"
        f"<th style='{_TH}'>Return period</th>"
        f"</tr></thead>"
        f"<tbody>{''.join(tbl_rows)}</tbody>"
        f"</table>"
    )
    already_passed_html = ""
    if already_passed_pairs:
        _passed_by_storm: dict[str, list[str]] = {}
        for (_ap_aid, _ap_iso3), _ap_last_t in sorted(already_passed_pairs.items()):
            _ap_t_str = _format_issued_et(_ap_last_t)
            _passed_by_storm.setdefault(_ap_aid, []).append(
                f"{_cname(_ap_iso3)} ({_ap_t_str})"
            )
        _passed_parts = []
        for _ap_aid, _ap_entries in _passed_by_storm.items():
            _ap_label = _storm_label(*storm_meta.get(_ap_aid, (None, None)))
            _passed_parts.append(f"{_ap_label}: {', '.join(sorted(_ap_entries))}")
        already_passed_html = (
            "<p style='font-size:0.88em;color:#5e6a6b;margin:0 0 20px'>"
            "<strong>Countries already passed</strong> "
            "(see past emails for final exposure estimate): "
            + "; ".join(_passed_parts)
            + "</p>"
        )

    # Intro block: which of ADAM / GDACS / OCHA CHD contributed exposure data.
    def _has_exposure(df) -> bool:
        return (
            df is not None and not df.empty
            and "pop_exposed" in df.columns and (df["pop_exposed"] > 0).any()
        )

    _chd = (
        _has_exposure(fcast_df) or _has_exposure(obsv_df) or _has_exposure(wsp_exp_df)
    )
    _src_flags = [
        ("ADAM", _has_exposure(adam_cur_df)),
        ("GDACS", _has_exposure(gdacs_cur_df)),
        ("OCHA CHD", _chd),
    ]
    _present = [s for s, ok in _src_flags if ok]
    _missing = [s for s, ok in _src_flags if not ok]
    if _missing:
        # Surfaces a source that didn't match this advisory's issued_time (e.g.
        # ADAM/GDACS publishing >3h late) so the consolidated MAX silently
        # degrading to fewer sources is at least visible in the run logs.
        logger.warning(
            f"No exposure matched for source(s) {_missing} at issued_time "
            f"{issued_time} (or -3h); consolidated figures use {_present} only."
        )

    storm_names = [
        _storm_label(storm_meta.get(aid, (None, None))[0], None)
        for aid in _ordered_aids
    ]
    _n = len(storm_names)
    _missing_sentence = (
        f" Estimates from {_oxford(_missing)} were not accessed for this email."
        if _missing else ""
    )
    _p = (
        "font-family:sans-serif;color:#1f2324;font-size:0.95em;"
        "line-height:1.55;margin:0 0 14px"
    )
    _detail_sentence = (
        "" if full else (
            " Full admin-1 figures are in the attached spreadsheet; "
            "<a href='https://ocha-dap.github.io/ds-storms-alerts/alerts/' "
            "style='color:#1862d8'>example alerts with the full charts</a> "
            "and <a href='https://ocha-dap.github.io/ds-storms-alerts/guide.html' "
            "style='color:#1862d8'>the methodology</a> are online."
        )
    )
    if full:
        # The online full-detail page is a reference document, not
        # correspondence — no salutation, just the advisory context.
        intro_html = (
            f"<p style='{_p}'>NHC cyclone forecasts issued "
            f"{_format_issued_et(issued_time_dt)} (NY time) &middot; "
            f"{_n} active storm{'' if _n == 1 else 's'}: "
            f"<b>{', '.join(storm_names)}</b>. "
            f"Consolidates exposure estimates from "
            f"{_oxford(_present)}.{_missing_sentence}</p>"
        )
    else:
        intro_html = (
            f"<p style='{_p}'>Dear colleagues,</p>"
            f"<p style='{_p}'>NHC has issued their cyclone forecasts for "
            f"{_format_issued_et(issued_time_dt)} (NY time). "
            f"There {'is' if _n == 1 else 'are'} {_n} active "
            f"storm{'' if _n == 1 else 's'}: <b>{', '.join(storm_names)}</b>.</p>"
            f"<p style='{_p}'>This email consolidates exposure estimates from "
            f"{_oxford(_present)}.{_missing_sentence}{_detail_sentence}</p>"
            f"<p style='{_p};margin-bottom:22px'>"
            f"Best regards,<br>OCHA Centre for Humanitarian Data</p>"
        )

    _hr = "<hr style='border:none;border-top:1px solid #e2e8e8;margin:26px 0'>"
    summary_header = f"<h2 style='{_H2}'>Summary</h2>"
    links_note = (
        "<p style='font-size:0.85em;color:#5e6a6b;margin:0 0 18px'>"
        "<a href='https://ocha-dap.github.io/ds-storms-alerts/guide.html' "
        "style='color:#1862d8'>Methodology &amp; documentation</a>"
        " &nbsp;|&nbsp; "
        "<a href='https://ocha-dap.github.io/ds-storms-alerts/' "
        "style='color:#1862d8'>Sign up for alerts</a>"
        " &nbsp;|&nbsp; "
        "<a href='https://ocha-dap.github.io/ds-storms-alerts/alerts/' "
        "style='color:#1862d8'>Example alerts</a>"
        "</p>"
    )
    body = intro_html + _hr + summary_header + toc_html + links_note + already_passed_html
    if sections:
        body += _hr + _hr.join(sections)
    return body, all_render_iso3s, storm_names


def _build_adm1_rows(
    aid: str,
    storm_iso3s: set[str],
    final_update_pairs: set[tuple[str, str]],
    iso3_to_name: dict[str, str],
    fm_name_by_pcode: dict[str, str],
    fcast_adm1_df,
    obsv_adm1_df,
    gdacs_adm1_df,
    adam_adm1_df,
) -> list[dict]:
    """Build admin-1 workbook rows for one storm, MAX-combining the sources per FM unit.

    Long format: one row per (iso3, fm_pcode, wind_speed_kt). Enumerates every
    (iso3, fm_pcode) seen in ANY adm1 source for this storm's countries, then
    for each wind speed takes the MAX across CHD (fcast + obsv), ADAM and
    GDACS — mirroring the admin-0 logic one level down.

    The set of sources is held **consistent across all admin-1 units within a
    storm-country** (per wind speed): a source counts as "used" for the country
    if it covers any of that country's units at that wind speed, and then every
    unit's figure is the MAX over that same set (a source that doesn't reach a
    given unit simply contributes 0). This keeps the units directly comparable —
    every row in a country reflects the same sources — rather than one unit being
    max(CHD, ADAM) and its neighbour ADAM-only. The numeric MAX is unchanged by
    this (adding zeros never changes a maximum); only the `sources` labels
    become uniform per country. Returns only units with at least one positive
    estimate.
    """
    import pandas as _pd

    f1 = fcast_adm1_df[fcast_adm1_df["atcf_id"] == aid]
    o1 = obsv_adm1_df[obsv_adm1_df["atcf_id"] == aid]
    g1 = gdacs_adm1_df[gdacs_adm1_df["atcf_id"] == aid]
    a1 = adam_adm1_df[adam_adm1_df["atcf_id"] == aid]

    def _match(df, iso3: str, pcode: str, wsp: int):
        if df.empty:
            return None
        sub = df[
            (df["iso3"] == iso3)
            & (df["fm_pcode"] == pcode)
            & (df["wind_speed_kt"] == wsp)
        ]
        return sub if not sub.empty else None

    def _num(df, iso3: str, pcode: str, wsp: int) -> int:
        sub = _match(df, iso3, pcode, wsp)
        return int(sub["pop_exposed"].iloc[0]) if sub is not None else 0

    def _caveat(df, iso3: str, pcode: str, wsp: int):
        sub = _match(df, iso3, pcode, wsp)
        if sub is None:
            return None
        val = sub["caveat_note"].iloc[0]
        return val if _pd.notna(val) else None

    # Every FM unit seen in any source for this storm's countries.
    adm1_keys: set[tuple[str, str]] = set()
    for src in (f1, o1, g1, a1):
        sub = src[src["iso3"].isin(storm_iso3s)]
        adm1_keys |= {
            (r.iso3, r.fm_pcode)
            for r in sub.itertuples()
            if _pd.notna(r.fm_pcode)
        }

    # First pass: per-unit source values, and the consistent set of sources used
    # for each (iso3, wind speed) — a source is "used" for the country if it
    # covers any unit there at that wind speed.
    unit_vals: dict[tuple[str, str, int], dict[str, int]] = {}
    sources_used: dict[tuple[str, int], set[str]] = {}
    for iso3, pcode in adm1_keys:
        for wsp in (34, 50, 64):
            vals = {
                "our": _num(f1, iso3, pcode, wsp) + _num(o1, iso3, pcode, wsp),
                "ADAM": _num(a1, iso3, pcode, wsp),
                "GDACS": _num(g1, iso3, pcode, wsp),
            }
            unit_vals[(iso3, pcode, wsp)] = vals
            used = sources_used.setdefault((iso3, wsp), set())
            used |= {k for k, v in vals.items() if v > 0}

    # Second pass: one row per (unit, wind threshold), MAX over the country's
    # consistent set. Units with no positive value at ANY threshold are
    # dropped entirely; kept units emit all three thresholds (zero rows
    # included, matching the archive workbook's keep-zeros convention).
    out: list[dict] = []
    for iso3, pcode in sorted(adm1_keys):
        if not any(v > 0 for wsp in (34, 50, 64)
                   for v in unit_vals[(iso3, pcode, wsp)].values()):
            continue
        for wsp in (34, 50, 64):
            vals = unit_vals[(iso3, pcode, wsp)]
            used = sources_used.get((iso3, wsp), set())
            # Iterate in canonical order so the source list is stable.
            ordered = [k for k in ("our", "ADAM", "GDACS") if k in used]
            unit_val = max(vals[k] for k in ordered) if ordered else 0
            cavs = [
                c for c in (
                    _caveat(g1, iso3, pcode, wsp),
                    _caveat(a1, iso3, pcode, wsp),
                ) if c
            ]
            out.append({
                "atcf_id": aid,
                "admin_level": 1,
                "country_name": iso3_to_name.get(iso3, iso3),
                "iso3": iso3,
                "admin_pcode": pcode,
                "admin_name": fm_name_by_pcode.get(pcode, pcode),
                "is_final_alert": (aid, iso3) in final_update_pairs,
                "wind_speed_kt": wsp,
                # Blank the source list when this unit has no exposure at this
                # wind speed, mirroring admin 0 — otherwise the consistent-
                # per-country set would list sources next to a 0 here (they
                # cover the country elsewhere but not this unit), reading
                # inconsistently across levels.
                "sources": (
                    "|".join(_SRC_LABELS[k] for k in ordered)
                    if unit_val > 0 else ""
                ),
                "pop_exposed": unit_val,
                "caveat": " | ".join(dict.fromkeys(cavs)),
            })
    return out


def _email_readme_blocks(storm_label, aid, issued_time_dt, adm0, adm1, cav):
    """README cover-sheet blocks for one storm's exposure workbook — the
    same block vocabulary as the archive workbook's README (see
    src/xlsx_style.build_readme)."""
    B = lambda k, t: (k, t)  # noqa: E731 (terse block builder)
    issued = issued_time_dt.strftime("%Y-%m-%d %H:00")
    return [
        B("title", f"Tropical Cyclone Population Exposure — {storm_label}"),
        B("subtitle", "Forecast-based estimates from CHD, GDACS and ADAM — "
          "admin 0 & admin 1"),
        B("meta", f"OCHA Data Science Unit  ·  storm {aid}  ·  NHC advisory "
          f"issued {issued} UTC"),
        B("gap", ""),
        B("h2", "What this file is"),
        B("body", "Live, forecast-based population-exposure estimates for this "
          "storm as of the advisory above: people inside the forecast wind "
          "footprint plus the track observed so far. Figures update with every "
          "advisory and can move up or down. The companion historical archive "
          "workbook (ds-storm-impact-harmonisation) reports each storm's FINAL "
          "observed footprint in the same layout, with the three sources side "
          "by side."),
        B("gap", ""),
        B("h2", "Tabs"),
        B("bullet", f"adm0_exposure — country level ({len(adm0)} rows): one "
          f"row per country × wind threshold (34/50/64 kt). The storm key on "
          f"both exposure tabs is atcf_id, the NHC ATCF identifier "
          f"(e.g. {aid})."),
        B("bullet", f"adm1_exposure — subnational FieldMaps units "
          f"({len(adm1)} rows): one row per admin-1 unit × wind threshold, "
          f"for units with any exposure."),
        B("bullet", f"caveats — GDACS/ADAM admin-1 alignment policy and "
          f"reviewer notes for this storm's countries ({len(cav)} rows), from "
          f"the FieldMaps lookups' own caveat system."),
        B("gap", ""),
        B("h2", "Reading the values"),
        B("bullet", "pop_exposed is the MAX across the sources reporting a "
          "positive value at that row's wind threshold (bias to action) — "
          "NOT a sum or mean. sources lists the contributing sources "
          "(e.g. CHD|ADAM|GDACS); blank means no source reports exposure "
          "there."),
        B("bullet", "CHD is our NHC-derived estimate: the current forecast "
          "footprint plus the track observed so far. GDACS (JRC) and ADAM "
          "(WFP) are those sources' live event estimates. GDACS has no 50 kt "
          "threshold."),
        B("bullet", "Admin-1 figures take the MAX per unit independently, so "
          "they do NOT necessarily sum to the country total. The caveat "
          "column flags GDACS/ADAM boundary-matching caveats for that unit."),
        B("bullet", "is_final_alert = TRUE marks the last update for that "
          "country: the storm no longer poses a forecast threat there and the "
          "figures reflect observed exposure."),
    ]


def _workbook_bytes(adm0_df, adm1_df, cav_df, readme_blocks) -> bytes:
    """Write the styled four-tab xlsx (README first) and return its bytes."""
    import pandas as _pd

    money = ["pop_exposed"]
    widths = {"storm_name": 16, "admin_name": 26, "country_name": 22,
              "sources": 17, "caveat": 30,
              "scope": 24, "adm1_alignment": 40, "caveat_kind": 22,
              "caveat_note": 60, "note": 90}
    buf = io.BytesIO()
    with _pd.ExcelWriter(buf, engine="openpyxl") as xl:
        adm0_df.to_excel(xl, sheet_name="adm0_exposure", index=False)
        adm1_df.to_excel(xl, sheet_name="adm1_exposure", index=False)
        cav_df.to_excel(xl, sheet_name="caveats", index=False)
        wb = xl.book
        readme = wb.create_sheet("README", 0)
        build_readme(readme, readme_blocks)
        for tab in ("adm0_exposure", "adm1_exposure"):
            style_data_sheet(
                wb[tab], money_cols=money,
                plain_cols=["season", "wind_speed_kt"],
                widths=widths, hidden=["admin_level"])
        style_data_sheet(wb["caveats"], widths=widths)
        wb.active = 0
    return buf.getvalue()


def generate_exposure_workbook(
    engine, issued_time_dt: datetime
) -> list[tuple[str, bytes]]:
    """Return a list of (filename, xlsx_bytes) — one styled Excel workbook per
    active or final-update storm.

    The workbook mirrors the historical archive workbook from
    ds-storm-impact-harmonisation — same tabs (README, adm0_exposure,
    adm1_exposure, caveats), same styling, same long layout (one row per
    admin unit × wind_speed_kt), same identity columns:
        adm0: atcf_id, storm_name, season, admin_level, iso3, admin_name
        adm1: + country_name, admin_pcode, admin_name
    It differs from the archive only where the products genuinely differ:
    the value block is a single pop_exposed (MAX across sources, bias to
    action) with `sources` ("CHD|ADAM|GDACS") and, at adm1, a `caveat`
    column — vs the archive's three per-source exposure columns; it is per
    storm per issued_time (so has no storms tab); and it is based on the
    current forecast (forecast footprint + track observed so far), whereas
    the archive reports each storm's final observed footprint.

    atcf_id is the NHC ATCF storm identifier (e.g. AL132025) — also in the
    filename, but carried as a column so concatenated tabs keep storm
    identity. At admin 1 the three sources are harmonized onto a common
    FieldMaps pcode (admin_pcode/admin_name), and the source set is held
    consistent across all admin-1 units within a storm-country (per wind
    speed) so the units are directly comparable — see _build_adm1_rows.
    Because MAX is taken per unit independently, admin 1 figures do NOT
    necessarily sum to the country total. The caveats tab carries the
    GDACS/ADAM admin-1 alignment policy + reviewer notes for this storm's
    countries, straight from the FM lookups' own caveat system.
    """
    import pandas as _pd

    fcast_df = fetch_fcast_exposure(engine, issued_time_dt)
    all_atcf_ids = fcast_df["atcf_id"].unique().tolist()

    prev_any_rows = fetch_prev_any_pairs(engine, issued_time_dt)
    prev_any_pairs = {(r["atcf_id"], r["iso3"]) for r in prev_any_rows}

    current_any_pairs = {(r.atcf_id, r.iso3) for r in fcast_df.itertuples()}
    final_update_pairs: set[tuple[str, str]] = prev_any_pairs - current_any_pairs

    all_fetch_ids = sorted(set(all_atcf_ids) | {aid for aid, _ in final_update_pairs})
    if not all_fetch_ids:
        return []

    obsv_df = fetch_current_obsv_exposure(engine, all_fetch_ids, issued_time_dt)
    obsv_pairs = {
        (r.atcf_id, r.iso3) for r in obsv_df.itertuples() if r.pop_exposed > 0
    }
    final_update_pairs = {p for p in final_update_pairs if p in obsv_pairs}

    gdacs_cur_df = fetch_gdacs_current_exposure(engine, all_fetch_ids, issued_time_dt)
    adam_cur_df = fetch_adam_current_exposure(engine, all_fetch_ids, issued_time_dt)

    # Admin-1 (subnational) exposure, harmonized onto FieldMaps pcodes.
    fcast_adm1_df = fetch_fcast_exposure_adm1(engine, issued_time_dt)
    obsv_adm1_df = fetch_current_obsv_exposure_adm1(engine, all_fetch_ids, issued_time_dt)
    gdacs_adm1_df = fetch_gdacs_current_exposure_adm1(engine, all_fetch_ids, issued_time_dt)
    adam_adm1_df = fetch_adam_current_exposure_adm1(engine, all_fetch_ids, issued_time_dt)

    # GDACS adm1 units with no FieldMaps match arrive as fm_pcode=NaN "orphan"
    # rows. Don't silently drop them — log a count + approximate population, then
    # exclude them (they can't be combined with the other sources by FM unit).
    # (ADAM orphans are dropped in SQL by fetch_adam_current_exposure_adm1.)
    _orphans = gdacs_adm1_df[gdacs_adm1_df["fm_pcode"].isna()]
    if not _orphans.empty:
        _n_units = _orphans["gdacs_admins"].nunique()
        # Wind-speed bands nest (everyone exposed at 64kt is also in the 34kt
        # band), so don't sum across them — that double/triple-counts. Take the
        # widest band per unit (max over wind speeds = the 34kt figure for
        # cumulative exposure) as a truer headcount.
        _orphan_pop = int(
            _orphans.groupby(["atcf_id", "iso3", "gdacs_admins"])["pop_exposed"]
            .max()
            .sum()
        )
        logger.warning(
            "Dropping %d GDACS adm1 unit(s) with no FieldMaps match "
            "(~%d pop in the widest wind band) from the exposure workbook.",
            _n_units, _orphan_pop,
        )
    gdacs_adm1_df = gdacs_adm1_df[gdacs_adm1_df["fm_pcode"].notna()].copy()

    # Storm metadata (name, season) for filenames
    meta: dict[str, tuple] = {}
    for _, row in fcast_df.drop_duplicates("atcf_id").iterrows():
        meta[row["atcf_id"]] = (row["name"], row["season"])
    for r in prev_any_rows:
        if r["atcf_id"] not in meta:
            meta[r["atcf_id"]] = (r["name"], r["season"])

    # Group pairs by storm
    storm_to_pairs: dict[str, list[tuple[str, str]]] = {}
    for aid, iso3 in current_any_pairs | final_update_pairs:
        storm_to_pairs.setdefault(aid, []).append((aid, iso3))

    # Country name lookup
    all_iso3s = sorted({iso3 for pairs in storm_to_pairs.values() for _, iso3 in pairs})
    adm1 = load_adm1_boundaries(all_iso3s)
    # Modal adm0_name per iso3 — NOT the first row's: FieldMaps gives some
    # outlying units their own adm0_name (JAM's first row is "Pedro Bank
    # (Jam.)", which used to label the whole country).
    iso3_to_name = (
        adm1.groupby("iso_3")["adm0_name"]
        .agg(lambda s: s.mode().iat[0]).to_dict()
    )
    # FieldMaps pcode -> name for labelling adm1 rows (falls back to bare pcode).
    fm_name_by_pcode = fetch_fm_names(engine, all_iso3s)

    def _obsv(aid: str, iso3: str, wsp: int) -> int:
        sub = obsv_df[
            (obsv_df["atcf_id"] == aid)
            & (obsv_df["iso3"] == iso3)
            & (obsv_df["wind_speed_kt"] == wsp)
        ]
        return int(sub["pop_exposed"].sum()) if not sub.empty else 0

    results: list[tuple[str, bytes]] = []
    cav_all = fetch_lookup_caveats(engine, all_iso3s)
    for aid in sorted(storm_to_pairs.keys()):
        nm, ssn = meta.get(aid, (None, None))
        storm_slug = _storm_label(nm, ssn).lower().replace(" ", "_")
        filename = f"{storm_slug}_{aid}_issued_{issued_time_dt.strftime('%Y-%m-%dT%H')}.xlsx"

        rows = []
        # --- admin 0 (country) rows: one per (country, wind threshold) ---
        for _, iso3 in sorted(storm_to_pairs[aid], key=lambda p: p[1]):
            is_final = (aid, iso3) in final_update_pairs
            for wsp in (34, 50, 64):
                tr = fcast_df[
                    (fcast_df["atcf_id"] == aid)
                    & (fcast_df["iso3"] == iso3)
                    & (fcast_df["wind_speed_kt"] == wsp)
                ]
                fcast_val = int(tr["pop_exposed"].iloc[0]) if not tr.empty else 0
                our_val = fcast_val + _obsv(aid, iso3, wsp)

                gr = gdacs_cur_df[
                    (gdacs_cur_df["atcf_id"] == aid)
                    & (gdacs_cur_df["iso3"] == iso3)
                    & (gdacs_cur_df["wind_speed_kt"] == wsp)
                ]
                gdacs_val = int(gr["pop_exposed"].iloc[0]) if not gr.empty else 0

                ar = adam_cur_df[
                    (adam_cur_df["atcf_id"] == aid)
                    & (adam_cur_df["iso3"] == iso3)
                    & (adam_cur_df["wind_speed_kt"] == wsp)
                ]
                adam_val = int(ar["pop_exposed"].iloc[0]) if not ar.empty else 0

                active = {
                    k: v for k, v in
                    {"our": our_val, "ADAM": adam_val, "GDACS": gdacs_val}.items()
                    if v > 0
                }
                # adm0: admin_name is the country name; admin_pcode == iso3 is
                # dropped as redundant (mirroring the archive workbook).
                # MAX across sources, not mean (bias to action — see the alert
                # ToC loop for the rationale).
                rows.append({
                    "atcf_id": aid,
                    "admin_level": 0,
                    "admin_name": iso3_to_name.get(iso3, iso3),
                    "iso3": iso3,
                    "is_final_alert": is_final,
                    "wind_speed_kt": wsp,
                    "sources": (
                        "|".join(_SRC_LABELS.get(k, k) for k in active)
                        if active else ""
                    ),
                    "pop_exposed": max(active.values()) if active else 0,
                })

        # --- admin 1 (subnational) rows ---
        adm1_rows = _build_adm1_rows(
            aid,
            {iso3 for _, iso3 in storm_to_pairs[aid]},
            final_update_pairs,
            iso3_to_name,
            fm_name_by_pcode,
            fcast_adm1_df,
            obsv_adm1_df,
            gdacs_adm1_df,
            adam_adm1_df,
        )

        adm0_df = _pd.DataFrame(rows).reindex(columns=_ADM0_COLS)
        adm1_df = _pd.DataFrame(adm1_rows).reindex(columns=_ADM1_COLS)
        try:
            season = int(ssn)
        except (TypeError, ValueError):
            season = ssn
        for df in (adm0_df, adm1_df):
            df["storm_name"] = nm if nm else aid
            df["season"] = season
        adm0_df = adm0_df.sort_values(["iso3", "wind_speed_kt"])
        adm1_df = adm1_df.sort_values(["iso3", "admin_pcode", "wind_speed_kt"])

        # caveats tab: this storm's countries only (the archive workbook
        # carries the full global set).
        storm_iso3s = sorted({i for _, i in storm_to_pairs[aid]})
        cav = cav_all[cav_all["iso3"].isin(storm_iso3s)].copy()
        cav["country_name"] = cav["iso3"].map(lambda i: iso3_to_name.get(i, i))
        cav["adm1_alignment"] = cav["caveat_kind"].map(
            lambda k: _ALIGN.get(k) or fm.CAVEAT_LABELS.get(k) or "see note")
        cav = cav.sort_values(
            ["source", "iso3", "admin_level", "scope"])[_CAVEAT_TAB_COLS]

        blocks = _email_readme_blocks(
            _storm_label(nm, ssn), aid, issued_time_dt, adm0_df, adm1_df, cav)
        results.append((filename, _workbook_bytes(adm0_df, adm1_df, cav, blocks)))

    return results


if __name__ == "__main__":
    args = parse_args()
    if args.issued_time:
        issued_time_dt = datetime.strptime(args.issued_time, "%Y-%m-%dT%H")
    else:
        issued_time_dt = _most_recent_advisory_time()
        logger.info(
            f"No --issued-time provided; defaulted to most recent advisory "
            f"hour {issued_time_dt.isoformat()}"
        )
    issued_time = issued_time_dt.strftime("%Y-%m-%dT%H")

    preview = args.preview
    stage = args.stage
    if args.send_test:
        # An explicit --send-test overrides the env switches rather than
        # requiring the caller to set two of them consistently; getting
        # TEST_EMAIL=False here would mail the live country lists.
        TEST_EMAIL, DRY_RUN = True, False
    logger.info(
        f"Starting alert pipeline: {issued_time=} {stage=} "
        f"{TEST_EMAIL=} {DRY_RUN=} {preview=}"
    )

    engine = stratus.get_engine(stage=stage)
    result = generate_alert_html(engine, issued_time_dt, full=args.full)

    if result is None:
        active_meta = fetch_active_storm_meta(engine, issued_time_dt)
        if not active_meta:
            logger.info("No active storms this advisory — nothing to send.")
            sys.exit(0)
        logger.info(
            f"Active storms but no exposure: {[m['atcf_id'] for m in active_meta]}"
        )
        body = generate_monitoring_html(engine, issued_time_dt, active_meta)
        active_iso3s: list[str] = []
        _names = [_storm_label(m["name"], None) for m in active_meta]
        is_monitoring = True
    else:
        body, active_iso3s, _names = result
        is_monitoring = False

    prefix = "[TEST] " if TEST_EMAIL else ""
    subject = _build_subject(issued_time_dt, _names, prefix=prefix)
    if is_monitoring:
        campaign_name = f"{prefix}ds-storms-alerts_monitoring_{issued_time}"
    else:
        campaign_name = f"{prefix}ds-storms-alerts_{issued_time}"

    if preview:
        html = None
        if not args.raw:
            try:
                html = render_with_template(body, subject, campaign_name)
                logger.info("Rendered through the Listmonk campaign template.")
            except PreviewUnavailable as exc:
                logger.warning(
                    f"Listmonk template unavailable ({exc}); "
                    f"falling back to the bare body."
                )
        if html is None:
            style = "font-family:sans-serif;max-width:900px;margin:auto"
            html = (
                "<html><head><meta charset='utf-8'></head>"
                f"<body style='{style}'>{body}</body></html>"
            )

        if args.out:
            path = Path(args.out).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
        else:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".html",
                prefix=f"storms_preview_{issued_time}_",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(html)
            path = Path(f.name)
        if not args.no_open:
            webbrowser.open(path.as_uri())
        logger.info(f"Preview written: {path}")
        sys.exit(0)

    if DRY_RUN:
        target = (
            "monitoring list only" if is_monitoring
            else f"countries {active_iso3s}"
        )
        logger.info(
            f"DRY_RUN=True — skipping email. "
            f"Would have sent: {subject!r} to {target}"
        )
    else:
        from ocha_relay.listmonk import ListmonkClient

        client = ListmonkClient.from_env()

        if args.send_test:
            # --send-test means the test list and nothing else. Falling through
            # to the monitoring branch here would mail the whole DSci monitoring
            # list from what the operator asked for as a test of one advisory.
            list_ids = TEST_LIST_IDS
        elif is_monitoring:
            list_ids = _fetch_monitoring_list_ids(client)
            if not list_ids:
                logger.info("No aggregate:monitoring list — skipping send.")
                sys.exit(0)
        elif TEST_EMAIL:
            list_ids = TEST_LIST_IDS
        else:
            logger.info(f"Resolving per-country lists for: {active_iso3s}")
            list_ids = resolve_country_list_ids(client, active_iso3s)
        logger.info(f"Targeting list IDs: {list_ids}")

        logger.info("Uploading images to listmonk media library...")
        _uploaded: dict[str, str] = {}

        def _upload_image(m: re.Match) -> str:
            b64 = m.group(1)
            if b64 not in _uploaded:
                _uploaded[b64] = client.upload_media(
                    base64.b64decode(b64), "chart.png"
                )
            return _uploaded[b64]

        body = re.sub(
            r'data:image/png;base64,([A-Za-z0-9+/=]+)',
            _upload_image,
            body,
        )
        logger.info(f"Uploaded {len(_uploaded)} images.")

        media_ids: list[int] = []
        if not is_monitoring:
            logger.info("Generating and uploading exposure workbook attachments...")
            attachments = generate_exposure_workbook(engine, issued_time_dt)
            for filename, xlsx_bytes in attachments:
                media_ids.append(client.upload_attachment(xlsx_bytes, filename))
                logger.info(f"  Attached {filename}")

        cid = client.create_campaign(
            name=campaign_name,
            subject=subject,
            body=body,
            list_ids=list_ids,
            media_ids=media_ids,
        )
        logger.info(f"Created campaign {cid}: {campaign_name!r}")
        client.send_campaign(cid, skip_confirmation=True)
        logger.info(f"Sent campaign {cid}")

    logger.info("Alert pipeline complete.")
