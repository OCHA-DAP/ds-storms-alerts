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
from datetime import datetime
from pathlib import Path

import ocha_stratus as stratus

from src.constants import PROD_LIST_IDS, TEST_LIST_IDS
from src.data import (
    fetch_adam_current_exposure,
    fetch_adam_historical_exposure,
    fetch_buffers,
    fetch_current_obsv_exposure,
    fetch_fcast_exposure,
    fetch_gdacs_current_exposure,
    fetch_gdacs_historical_exposure,
    fetch_historical_obsv_exposure,
    fetch_track_geo,
    fetch_prev_any_pairs,
    fetch_wsp_fcastonly_exposure,
    fetch_wsp_fcastonly_polygons,
    load_adm1_boundaries,
    load_background_countries,
)
from src.plots import (
    StormMark,
    WspPdf,
    adam_strip_chart,
    country_strip_chart,
    gdacs_strip_chart,
    track_plot_buffers,
    track_plot_wsp,
    wind_speed_color,
)

_HIST_COLOR = "#888888"

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_H2 = "font-size:1.35em;margin:24px 0 8px;font-weight:600"
_H3 = "font-size:1.1em;margin:16px 0 6px;font-weight:600;color:#444"
_H4 = "font-size:0.95em;margin:10px 0 4px;font-weight:600;color:#666"
_H5 = "font-size:0.85em;margin:8px 0 3px;font-weight:600;color:#888"


def _parse_bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name, "")
    if val == "":
        return default
    return val.strip().lower() not in ("false", "0", "no")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--issued-time",
        required=True,
        help="Issued time of the forecast, format YYYY-MM-DDTHH (e.g. 2025-01-15T12)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Generate HTML and open in browser; skip email entirely.",
    )
    return parser.parse_args()


TEST_EMAIL = _parse_bool_env("TEST_EMAIL", default=True)
DRY_RUN = _parse_bool_env("DRY_RUN", default=True)


def generate_alert_html(engine, issued_time_dt: datetime) -> str | None:
    """Run the full pipeline and return the email body HTML.

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

    logger.info("Fetching GDACS current exposure...")
    gdacs_cur_df = fetch_gdacs_current_exposure(engine, all_render_atcf_ids)

    logger.info("Fetching GDACS historical exposure...")
    gdacs_hist_df = fetch_gdacs_historical_exposure(
        engine, all_render_iso3s, exclude_atcf_ids=all_render_atcf_ids
    )
    gdacs_hist_df = gdacs_hist_df[gdacs_hist_df["season"] >= 2002].reset_index(drop=True)

    logger.info("Fetching ADAM current exposure...")
    adam_cur_df = fetch_adam_current_exposure(engine, all_render_atcf_ids)

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
    adm1_gdf = load_adm1_boundaries(all_render_iso3s)
    iso3_to_name: dict[str, str] = (
        adm1_gdf.groupby("iso_3")["adm0_name"]
        .agg(lambda x: x.value_counts().index[0])
        .to_dict()
    )

    def _cname(iso3: str) -> str:
        return iso3_to_name.get(iso3, iso3)

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
        if rp is None or rp <= 3:
            return ""
        if rp > 10:
            return "#ffcccc"
        if rp > 5:
            return "#ffe8cc"
        return "#fff9c4"

    def _rp_text(forecast_val: float, iso3: str, wsp: int) -> str:
        rp = _rp_numeric(forecast_val, iso3, wsp)
        if rp is None:
            return ""
        hist_vals = hist_df[
            (hist_df["iso3"] == iso3) & (hist_df["wind_speed_kt"] == wsp)
        ]["pop_exposed"].tolist()
        exceedances = sum(1 for v in hist_vals if v >= forecast_val)
        return (
            f"≈{rp:.0f}-year RP "
            f"({exceedances} storms since 2002 had ≥ this exposure)"
        )

    toc_storms: list[dict] = []

    for aid in sorted(storm_to_iso3s.keys(), key=lambda a: -_storm_exposure_score(a)):
        s_name, s_season = storm_meta.get(aid, (None, None))
        storm_h2_label = _storm_label(s_name, s_season)

        # Pre-filter DataFrames to this storm for per-storm mark computation.
        aid_obsv_df = obsv_df[obsv_df["atcf_id"] == aid]
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
        season_aid = tr_storm["season"].iloc[0] if not tr_storm.empty else s_season

        # Per-storm maps (WSP + buffers filtered to this storm only).
        aid_tracks = tracks_gdf[tracks_gdf["atcf_id"] == aid]
        aid_buffers = buffers_gdf[buffers_gdf["atcf_id"] == aid]
        aid_wsp_poly = wsp_gdf[wsp_gdf["atcf_id"] == aid]
        aid_adm1 = adm1_gdf[adm1_gdf["iso_3"].isin(storm_to_iso3s[aid])]
        storm_map_parts: list[str] = []
        wsp_m = track_plot_wsp(
            aid_tracks, aid_buffers, aid_wsp_poly, background_gdf,
            wind_threshold_kt=34, adm1_gdf=aid_adm1,
        )
        if wsp_m:
            storm_map_parts.append(f"<h3 style='{_H3}'>WSP 34 kt forecast</h3>{wsp_m}")
        buf_m = track_plot_buffers(aid_tracks, aid_buffers, background_gdf, adm1_gdf=aid_adm1)
        if buf_m:
            storm_map_parts.append(
                f"<h3 style='{_H3}'>Forecast-only buffers</h3>{buf_m}"
            )

        toc_countries: list[dict] = []
        country_sections: list[str] = []
        for iso3 in sorted(storm_to_iso3s[aid], key=lambda c: -_country_exposure_score(aid, c)):
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
                    f"<p style='background:#fff3cd;border-left:4px solid #ffc107;"
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
                _tot = _fv + _ov
                if _tot > 0:
                    _toc_wsps.append({
                        "wsp": _tw,
                        "total": _tot,
                        "rp": _rp_numeric(float(_tot), iso3, _tw),
                    })
            if _toc_wsps:
                toc_countries.append({
                    "name": _cname(iso3),
                    "is_final": (aid, iso3) in final_update_pairs,
                    "wsps": _toc_wsps,
                })

            ours_blocks: list[str] = []
            gdacs_blocks: list[str] = []
            adam_blocks: list[str] = []

            for wsp in active_wsps:
                wsp_color = wind_speed_color(wsp)
                obsv_floor = _obsv_for(obsv_df, aid, iso3, wsp)

                # Forecast mark: deterministic track fcastonly + observed only.
                # WSP is shown as the PDF shading only, not as a mark.
                fcast_total_marks: list[StormMark] = []
                tr_row = fcast_df[
                    (fcast_df["atcf_id"] == aid)
                    & (fcast_df["iso3"] == iso3)
                    & (fcast_df["wind_speed_kt"] == wsp)
                ]
                if not tr_row.empty and tr_row["pop_exposed"].iloc[0] > 0:
                    fcast_total_marks.append(StormMark(
                        value=int(tr_row["pop_exposed"].iloc[0]) + obsv_floor,
                        label=_storm_label(
                            name_aid, season_aid, "forecast + observed",
                        ),
                        color=wsp_color,
                    ))

                obsv_marks_list = _marks(
                    aid_obsv_df, iso3, wsp, wsp_color, "observed up to present",
                )
                ours_current_values = (
                    [m.value for m in obsv_marks_list]
                    + [m.value for m in fcast_total_marks]
                )
                hist_marks = _filter_historical(
                    _marks(hist_df, iso3, wsp, _HIST_COLOR, short=True),
                    x_max_per_wsp[wsp],
                    current_values=ours_current_values,
                )
                ours_marks = hist_marks + obsv_marks_list + fcast_total_marks

                # WSP PDF filtered to this storm only.
                wsp_sub = wsp_exp_df[
                    (wsp_exp_df["atcf_id"] == aid)
                    & (wsp_exp_df["iso3"] == iso3)
                    & (wsp_exp_df["wind_threshold_kt"] == wsp)
                ]
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

                ours_img = country_strip_chart(
                    iso3, wsp, ours_marks, x_max=x_max_per_wsp[wsp], pdf=pdf,
                )
                _ft_val = (
                    fcast_total_marks[0].value
                    if fcast_total_marks
                    else float(obsv_floor)
                )
                _rp = _rp_text(_ft_val, iso3, wsp)
                _rp_html = (
                    f"<p style='font-size:0.78em;color:#666;"
                    f"margin:-4px 0 10px;padding-left:2px'>{_rp}</p>"
                    if _rp else ""
                )
                ours_blocks.append(
                    f"<h5 style='{_H5}'>{wsp} kt</h5>{ours_img}{_rp_html}"
                )

                gdacs_cur_marks = _marks(aid_gdacs_cur, iso3, wsp, wsp_color, "GDACS")
                gdacs_marks = (
                    _filter_historical(
                        _marks(gdacs_hist_df, iso3, wsp, _HIST_COLOR, short=True),
                        x_max_per_wsp[wsp],
                        current_values=[m.value for m in gdacs_cur_marks],
                    )
                    + gdacs_cur_marks
                )
                gdacs_img = gdacs_strip_chart(
                    iso3, wsp, gdacs_marks, x_max=x_max_per_wsp[wsp],
                )
                gdacs_blocks.append(f"<h5 style='{_H5}'>{wsp} kt</h5>{gdacs_img}")

                adam_cur_marks = _marks(aid_adam_cur, iso3, wsp, wsp_color, "ADAM")
                adam_marks = (
                    _filter_historical(
                        _marks(adam_hist_df, iso3, wsp, _HIST_COLOR, short=True),
                        x_max_per_wsp[wsp],
                        current_values=[m.value for m in adam_cur_marks],
                    )
                    + adam_cur_marks
                )
                adam_img = adam_strip_chart(
                    iso3, wsp, adam_marks, x_max=x_max_per_wsp[wsp],
                )
                adam_blocks.append(f"<h5 style='{_H5}'>{wsp} kt</h5>{adam_img}")

            country_sections.append(
                f"<h3 style='{_H3}'>{_cname(iso3)}</h3>"
                + notice_html
                + f"<h4 style='{_H4}'>Our estimates</h4>{''.join(ours_blocks)}"
                + f"<h4 style='{_H4}'>ADAM</h4>{''.join(adam_blocks)}"
                + f"<h4 style='{_H4}'>GDACS</h4>{''.join(gdacs_blocks)}"
            )

        if toc_countries:
            toc_storms.append({"label": storm_h2_label, "countries": toc_countries})

        if storm_map_parts or country_sections:
            sections.append(
                f"<h2 style='{_H2}'>{storm_h2_label}</h2>"
                + "".join(storm_map_parts)
                + "".join(country_sections)
            )

    _TD = "padding:6px 10px;border:1px solid #ddd;vertical-align:top"
    _TH = (
        "padding:6px 10px;border:1px solid #ddd;background:#f0f0f0;"
        "text-align:left;font-weight:600;white-space:nowrap"
    )
    tbl_rows: list[str] = []
    for _st in toc_storms:
        _st_total_rows = sum(len(_c["wsps"]) for _c in _st["countries"])
        _st_max_rp = max(
            (_w["rp"] for _c in _st["countries"] for _w in _c["wsps"] if _w["rp"]),
            default=None,
        )
        _st_color = _rp_color(_st_max_rp)
        _st_first = True
        for _c in _st["countries"]:
            _c_rows = len(_c["wsps"])
            _c_max_rp = max((_w["rp"] for _w in _c["wsps"] if _w["rp"]), default=None)
            _c_color = _rp_color(_c_max_rp)
            _c_name = _c["name"]
            if _c["is_final"]:
                _c_name += (
                    " <em style='font-weight:normal;color:#888;"
                    "font-size:0.85em'>(final)</em>"
                )
            _c_first = True
            for _w in _c["wsps"]:
                _rc = _rp_color(_w["rp"])
                _rp_str = f"≈{_w['rp']:.0f}-year RP" if _w["rp"] else "—"
                _row = "<tr>"
                if _st_first:
                    _bg = _st_color or "#fafafa"
                    _row += (
                        f"<td rowspan='{_st_total_rows}' style='{_TD};"
                        f"background:{_bg};font-weight:600'>"
                        f"{_st['label']}</td>"
                    )
                    _st_first = False
                if _c_first:
                    _bg = _c_color or "#fff"
                    _row += (
                        f"<td rowspan='{_c_rows}' style='{_TD};"
                        f"background:{_bg}'>{_c_name}</td>"
                    )
                    _c_first = False
                _cell_bg = _rc or "#fff"
                _row += (
                    f"<td style='{_TD};text-align:center'>{_w['wsp']} kt</td>"
                    f"<td style='{_TD};background:{_cell_bg};"
                    f"text-align:right'>{_fmt_pop_toc(_w['total'])}</td>"
                    f"<td style='{_TD};background:{_cell_bg}'>{_rp_str}</td>"
                    f"</tr>"
                )
                tbl_rows.append(_row)

    toc_html = (
        f"<table style='width:100%;border-collapse:collapse;"
        f"margin:0 0 28px;font-size:0.88em'>"
        f"<thead><tr>"
        f"<th style='{_TH}'>Storm</th>"
        f"<th style='{_TH}'>Country</th>"
        f"<th style='{_TH}'>Wind</th>"
        f"<th style='{_TH}'>Exposure</th>"
        f"<th style='{_TH}'>Return period</th>"
        f"</tr></thead>"
        f"<tbody>{''.join(tbl_rows)}</tbody>"
        f"</table>"
    )
    return toc_html + "\n".join(sections)


def generate_exposure_csv(
    engine, issued_time_dt: datetime
) -> list[tuple[str, bytes]]:
    """Return a list of (filename, csv_bytes) — one per active or final-update storm.

    Each CSV has one row per country with columns:
        country, iso3, is_final_alert,
        pop_exposed_34kt, pop_exposed_50kt, pop_exposed_64kt
    where pop_exposed = fcastonly + obsv (deterministic only).
    """
    import pandas as _pd

    fcast_df = fetch_fcast_exposure(engine, issued_time_dt)
    all_atcf_ids = fcast_df["atcf_id"].unique().tolist()

    prev_any_rows = fetch_prev_any_pairs(engine, issued_time_dt)
    prev_any_pairs = {(r["atcf_id"], r["iso3"]) for r in prev_any_rows}
    prev_atcf_ids = sorted({r["atcf_id"] for r in prev_any_rows})

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
    iso3_to_name = (
        adm1.drop_duplicates("iso_3").set_index("iso_3")["adm0_name"].to_dict()
    )

    def _obsv(aid: str, iso3: str, wsp: int) -> int:
        sub = obsv_df[
            (obsv_df["atcf_id"] == aid)
            & (obsv_df["iso3"] == iso3)
            & (obsv_df["wind_speed_kt"] == wsp)
        ]
        return int(sub["pop_exposed"].sum()) if not sub.empty else 0

    results: list[tuple[str, bytes]] = []
    for aid in sorted(storm_to_pairs.keys()):
        nm, ssn = meta.get(aid, (None, None))
        storm_slug = _storm_label(nm, ssn).lower().replace(" ", "_")
        filename = f"{storm_slug}_{aid}_issued_{issued_time_dt.strftime('%Y-%m-%dT%H')}.csv"

        rows = []
        for _, iso3 in sorted(storm_to_pairs[aid], key=lambda p: p[1]):
            is_final = (aid, iso3) in final_update_pairs
            row: dict = {
                "country": iso3_to_name.get(iso3, iso3),
                "iso3": iso3,
                "is_final_alert": is_final,
            }
            for wsp in (34, 50, 64):
                tr = fcast_df[
                    (fcast_df["atcf_id"] == aid)
                    & (fcast_df["iso3"] == iso3)
                    & (fcast_df["wind_speed_kt"] == wsp)
                ]
                fcast_val = int(tr["pop_exposed"].iloc[0]) if not tr.empty else 0
                row[f"pop_exposed_{wsp}kt"] = fcast_val + _obsv(aid, iso3, wsp)
            rows.append(row)

        buf = io.StringIO()
        _pd.DataFrame(rows).to_csv(buf, index=False)
        results.append((filename, buf.getvalue().encode()))

    return results


if __name__ == "__main__":
    args = parse_args()
    issued_time = args.issued_time
    issued_time_dt = datetime.strptime(issued_time, "%Y-%m-%dT%H")

    preview = args.preview
    logger.info(
        f"Starting alert pipeline: {issued_time=} {TEST_EMAIL=} {DRY_RUN=} {preview=}"
    )

    list_ids = TEST_LIST_IDS if TEST_EMAIL else PROD_LIST_IDS

    engine = stratus.get_engine(stage="dev")
    body = generate_alert_html(engine, issued_time_dt)

    if body is None:
        logger.info("No countries with non-zero 64kt exposure — nothing to send.")
        sys.exit(0)

    prefix = "[TEST] " if TEST_EMAIL else ""
    subject = f"{prefix}Storm alert: {issued_time}"
    campaign_name = f"{prefix}ds-storms-alerts_{issued_time}"

    if preview:
        style = "font-family:sans-serif;max-width:900px;margin:auto"
        html = f"<html><body style='{style}'>{body}</body></html>"
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".html",
            prefix=f"storms_preview_{issued_time}_",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(html)
        path = Path(f.name)
        webbrowser.open(path.as_uri())
        logger.info(f"Preview opened: {path}")
        sys.exit(0)

    if DRY_RUN:
        logger.info(
            f"DRY_RUN=True — skipping email. "
            f"Would have sent: {subject!r} to lists {list_ids}"
        )
    else:
        from ocha_relay.listmonk import ListmonkClient

        client = ListmonkClient.from_env()

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

        logger.info("Generating and uploading CSV attachments...")
        csv_files = generate_exposure_csv(engine, issued_time_dt)
        media_ids: list[int] = []
        for filename, csv_bytes in csv_files:
            media_ids.append(client.upload_attachment(csv_bytes, filename))
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
