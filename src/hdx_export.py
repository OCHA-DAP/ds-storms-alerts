"""Per-storm population-exposure CSVs for HDX, parked on blob.

The HDX Data Systems pipeline OCHA-DAP/hdx-scraper-storms publishes one HDX
dataset per storm of the current NHC season. It used to query the storms
database itself; since the database went private-endpoint only (2026-09-30)
GitHub Actions can't reach it. So the database half now runs here, on
Databricks next to the alert, and the scraper publishes from blob (HDX's bot
token never leaves GitHub — same split as hdx-floodscan):

    {root}/{YYYY-MM-DDTHH}/{atcf_id}.csv    one per storm with track exposure
                                            (now or at an earlier advisory)
    {root}/{YYYY-MM-DDTHH}/manifest.json
    {root}/latest/manifest.json             written LAST

`root` is BLOB_ROOT (or BLOB_ROOT_TEST for test runs) on the DEV storage
account, which the scraper's workflow reads with the DSCI_AZ_BLOB_DEV_SAS org
secret. The CSVs in a dated folder are never rewritten by a later advisory,
and the manifest lists the blob of each storm's CSV, so a reader that opens
latest/manifest.json always gets one complete, consistent set.

The CSV layout is the scraper's own, unchanged (its exposure.py, which was
itself adapted from generate_exposure_workbook in pipelines/run_alert.py):
one row per admin unit (admin 0 and FieldMaps admin 1), with pop_exposed /
sources / caveat columns per wind threshold, pop_exposed being the MAX across
CHD (forecast + observed so far), ADAM and GDACS. Figures are taken at the
storm's own latest advisory, so a storm that has ended keeps its last numbers.

A near miss (forecast over land at some advisory, never observed there) has
no exposure at all once its forecast moves off, and would drop out of the
manifest: its HDX dataset would then freeze at the last forecast, still due
"every day" and never final. So a storm stays in once it has had exposure,
reporting the countries of its most recent advisory with forecast exposure
at the current figures — zero from CHD — final once nothing threatens them
(see fetch_near_miss_iso3s).

is_final_alert differs from the alert workbook's on purpose. There it means
"this email is the final update for the pair" and fires once, in the run
after the pair drops out. On HDX it is a standing property of the row: the
country is no longer threatened, so its figures are final. It is TRUE for
every country of the storm that is not still active at the CURRENT advisory
(`as_of`), with "still active" defined exactly as in the alert: track
forecast OR WSP band exposure. A storm with no advisory at `as_of` is
therefore final everywhere — the scraper's earlier per-storm test compared a
storm's last advisory with the one before it, so a storm that dissipated
while still forecast over a country stayed FALSE for good. The flip side is
the alert's own race: if the storms pipeline has not landed the `as_of`
advisory when this runs, a live storm looks ended for one cycle. The job runs
well after the NHC run for that reason.

latest/ only moves forward: a backfill run (an older issued_time) writes its
dated folder but leaves latest/ alone.

South Pacific (RSMC Nadi / FMS) storms are exported the same way under their
own root (BLOB_ROOT_SP), by a second task of the same scheduled job. What
differs:

* The first column is `storm_id`, the FMS disturbance id (e.g. 2526-11F):
  these storms have no ATCF id. The other columns are the NHC ones.
* CHD is the only source (no GDACS or ADAM figures for the Pacific), so the
  sources columns hold "CHD" or nothing and the caveat columns are empty.
* The season is the Southern Hemisphere one, July to June, named by the year
  it ends.
* FMS forecasts arrive one storm at a time, at no fixed hour, so `as_of` is
  the hour of the run. A storm is still active if it has an Official
  forecast issued in the 24 hours up to `as_of` (SP_ENDED_AFTER) and that
  forecast, or one of its track-uncertainty scenarios, puts people in the
  country: the scenarios play the WSP's part. The alert's own "current"
  window is 12 hours; here one skipped or late forecast must not mark a live
  storm final on a public dataset, and going final half a day later costs
  nothing. Running on the NHC job's six-hourly schedule, not on each
  forecast, is what lets a storm go final at all: FMS just stops sending,
  and no forecast means no run of the South Pacific alert job.
* Only forecasts whose processing has finished, scenarios included
  (SP_DONE_STAGE), are read. The pipeline marks a forecast's exposure done
  before it computes the scenarios; a run in between would find no scenario
  threat and call a country final that a shifted track still reaches.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

import ocha_stratus as stratus
import pandas as pd
from azure.core.exceptions import ResourceNotFoundError
from sqlalchemy import Engine, text

from src import data_sp
from src.basins import NHC, SP, BasinProfile
from src.constants import PROJECT_PREFIX
from src.data import (
    fetch_fcast_exposure,
    fetch_wsp_fcastonly_exposure,
    load_adm1_boundaries,
)

logger = logging.getLogger(__name__)

BLOB_STAGE = "dev"
BLOB_ROOT = f"{PROJECT_PREFIX}/processed/hdx"
# Test runs (the dev bundle target, --send-test style manual runs) write here,
# so the scraper never publishes a feature branch's output.
BLOB_ROOT_TEST = f"{PROJECT_PREFIX}/processed/hdx_test"
# South Pacific storms: their own manifest, read separately by the scraper.
BLOB_ROOT_SP = f"{PROJECT_PREFIX}/processed/hdx_sp"
BLOB_ROOT_SP_TEST = f"{PROJECT_PREFIX}/processed/hdx_sp_test"

_WIND_SPEEDS_KT = (34, 50, 64)
_SRC_LABELS = {"our": "CHD", "ADAM": "ADAM", "GDACS": "GDACS"}

# Column order of the per-storm CSV — the HDX resource's schema. Changing it
# changes the published dataset: coordinate with hdx-scraper-storms.
HDX_CSV_COLS = [
    "atcf_id",
    "admin_level",
    "iso3",
    "country_name",
    "admin_pcode",
    "admin_name",
    "is_final_alert",
    "pop_exposed_34kt",
    "pop_exposed_50kt",
    "pop_exposed_64kt",
    "sources_34kt",
    "sources_50kt",
    "sources_64kt",
    "caveat_34kt",
    "caveat_50kt",
    "caveat_64kt",
]
# South Pacific storms have no ATCF id: the FMS disturbance id goes in a
# column named for what it is. Everything after it is the same.
HDX_CSV_COLS_SP = ["storm_id", *HDX_CSV_COLS[1:]]
# A South Pacific storm has ended once its last Official forecast is this old.
SP_ENDED_AFTER = timedelta(hours=24)
# Last stage ds-storms-pipeline marks in storms.fms_processing_completion for
# a forecast (buffers, exposure, scenarios): the forecast is complete.
SP_DONE_STAGE = "scenarios"


def fetch_season_storms(engine: Engine, season: int, as_of: datetime) -> list[dict]:
    """Storms of `season` in the NHC registry, each with its latest advisory
    on or before `as_of` (from nhc_tracks_geo).

    Returns [{atcf_id, name, season, issued_time}, ...]. Storms with no
    advisory by `as_of` are left out.
    """
    sql = text("""
        SELECT s.atcf_id, NULLIF(s.name, 'NaN') AS name, s.season,
               MAX(t.issued_time) AS issued_time
        FROM storms.nhc_storms s
        JOIN storms.nhc_tracks_geo t ON t.atcf_id = s.atcf_id
        WHERE s.season = :season
          AND t.issued_time <= :as_of
        GROUP BY s.atcf_id, s.name, s.season
        ORDER BY s.atcf_id
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"season": season, "as_of": as_of}).fetchall()
    return [
        {"atcf_id": r[0], "name": r[1], "season": r[2], "issued_time": r[3]}
        for r in rows
    ]


def fetch_active_pairs(
    engine: Engine, atcf_ids: list[str], as_of: datetime
) -> set[tuple[str, str]]:
    """(atcf_id, iso3) pairs still under threat at `as_of`: track forecast OR
    WSP band exposure — the alert's definition of "still active"
    (generate_exposure_workbook's current_any_pairs)."""
    fcast_df = fetch_fcast_exposure(engine, as_of)
    wsp_df = fetch_wsp_fcastonly_exposure(engine, atcf_ids, as_of)
    return {
        (r.atcf_id, r.iso3)
        for r in fcast_df.itertuples()
        if r.atcf_id in atcf_ids
    } | {(r.atcf_id, r.iso3) for r in wsp_df.itertuples() if r.pop_exposed > 0}


def fetch_near_miss_iso3s(
    engine: Engine, atcf_id: str, issued_time: datetime, as_of: datetime
) -> list[str]:
    """Countries to keep reporting for a storm with no exposure left at its
    latest advisory `issued_time`: those of its most recent advisory with
    positive admin-0 forecast exposure. [] if it never had any.

    Also [] while the storm is current (`issued_time` no more than one cycle,
    6 h, before `as_of`, so a late storms-pipeline run is covered too) and
    fewer than two main advisories (03/09/15/21Z) have come since: the newest
    one's exposure may just not have landed yet, and reporting zeros for a
    storm about to make landfall would be worse than sitting out a cycle.
    Only main advisories count because nhc_tracks_geo also holds 00/06/12/18Z
    advisories, for which exposure is hardly ever computed. Once the storm
    is no longer current its last-threatened countries are reported anyway.
    """
    sql = text("""
        SELECT issued_time, iso3
        FROM storms.nhc_tracks_fcastonly_exposure
        WHERE atcf_id = :atcf_id
          AND admin_level = 0
          AND pop_exposed > 0
          AND issued_time = (
              SELECT MAX(issued_time)
              FROM storms.nhc_tracks_fcastonly_exposure
              WHERE atcf_id = :atcf_id
                AND admin_level = 0
                AND pop_exposed > 0
                AND issued_time <= :issued_time
          )
    """)
    since_sql = text("""
        SELECT COUNT(DISTINCT issued_time)
        FROM storms.nhc_tracks_geo
        WHERE atcf_id = :atcf_id
          AND issued_time > :last_exposed
          AND issued_time <= :issued_time
          AND EXTRACT(HOUR FROM issued_time) IN (3, 9, 15, 21)
          AND EXTRACT(MINUTE FROM issued_time) = 0
    """)
    params = {"atcf_id": atcf_id, "issued_time": issued_time}
    with engine.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []
        last_exposed = rows[0][0]
        if issued_time >= as_of - timedelta(hours=6):
            n_since = conn.execute(
                since_sql, {**params, "last_exposed": last_exposed}
            ).scalar()
            if n_since < 2:
                logger.info(
                    "HDX export: %s has no exposure at %s, last at %s; "
                    "waiting for the next advisory before reporting it",
                    atcf_id, issued_time, last_exposed,
                )
                return []
    return sorted({r[1] for r in rows})


def fetch_season_storms_sp(engine: Engine, season: int, as_of: datetime) -> list[dict]:
    """South Pacific counterpart of fetch_season_storms: the FMS disturbances
    of `season`, each with its latest Official forecast on or before `as_of`
    that has been processed in full (SP_DONE_STAGE).

    Returns the same keys, `atcf_id` holding the FMS disturbance id.
    """
    sql = text("""
        SELECT s.disturbance_id, s.name, s.season, MAX(f.issued_time) AS issued_time
        FROM storms.fms_storms s
        JOIN storms.fms_forecasts f ON f.disturbance_id = s.disturbance_id
        JOIN storms.fms_processing_completion c
          ON c.disturbance_id = f.disturbance_id
         AND c.issued_time = f.issued_time
         AND c.stage = :stage
        WHERE s.season = :season
          AND f.track_type = :official
          AND f.issued_time <= :as_of
        GROUP BY s.disturbance_id, s.name, s.season
        ORDER BY s.disturbance_id
    """)
    params = {
        "season": season,
        "as_of": as_of,
        "official": data_sp.OFFICIAL_TRACK,
        "stage": SP_DONE_STAGE,
    }
    with engine.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {"atcf_id": r[0], "name": r[1], "season": r[2], "issued_time": r[3]}
        for r in rows
    ]


def fetch_active_pairs_sp(
    engine: Engine, atcf_ids: list[str], as_of: datetime
) -> set[tuple[str, str]]:
    """(disturbance id, iso3) pairs still under threat at `as_of`: the
    storm's latest fully processed Official forecast was issued in the last
    SP_ENDED_AFTER, and it or one of its track-uncertainty scenarios exposes
    people in the country (admin 0). The alert's "still active" test, on a
    longer window (see the module docstring)."""
    sql = text("""
        WITH cur AS (
            SELECT f.disturbance_id, MAX(f.issued_time) AS it
            FROM storms.fms_forecasts f
            JOIN storms.fms_processing_completion c
              ON c.disturbance_id = f.disturbance_id
             AND c.issued_time = f.issued_time
             AND c.stage = :stage
            WHERE f.track_type = :official
              AND f.issued_time <= :as_of
            GROUP BY f.disturbance_id
        )
        SELECT e.disturbance_id, e.iso3
        FROM storms.fms_tracks_fcastonly_exposure e
        JOIN cur ON cur.disturbance_id = e.disturbance_id AND cur.it = e.issued_time
        WHERE cur.it > :t_min AND e.admin_level = 0 AND e.pop_exposed > 0
        UNION
        SELECT e.disturbance_id, e.iso3
        FROM storms.fms_tracks_scenario_exposure e
        JOIN cur ON cur.disturbance_id = e.disturbance_id AND cur.it = e.issued_time
        WHERE cur.it > :t_min AND e.admin_level = 0 AND e.pop_exposed > 0
    """)
    params = {
        "as_of": as_of,
        "t_min": as_of - SP_ENDED_AFTER,
        "official": data_sp.OFFICIAL_TRACK,
        "stage": SP_DONE_STAGE,
    }
    with engine.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {(r[0], r[1]) for r in rows if r[0] in atcf_ids}


def fetch_near_miss_iso3s_sp(
    engine: Engine, atcf_id: str, issued_time: datetime, as_of: datetime
) -> list[str]:
    """South Pacific counterpart of fetch_near_miss_iso3s: the countries of
    the storm's most recent forecast with positive admin-0 forecast exposure,
    [] if it never had any.

    No waiting here. A storm's latest forecast is by definition one that
    has been processed in full (fetch_season_storms_sp), so there is no
    advisory whose figures may not have landed yet.
    """
    sql = text("""
        SELECT DISTINCT iso3
        FROM storms.fms_tracks_fcastonly_exposure
        WHERE disturbance_id = :id
          AND admin_level = 0
          AND pop_exposed > 0
          AND issued_time = (
              SELECT MAX(issued_time)
              FROM storms.fms_tracks_fcastonly_exposure
              WHERE disturbance_id = :id
                AND admin_level = 0
                AND pop_exposed > 0
                AND issued_time <= :issued_time
          )
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"id": atcf_id, "issued_time": issued_time}).fetchall()
    return sorted(r[0] for r in rows)


@dataclass(frozen=True)
class _Export:
    """What differs between the basins' exports."""

    root: str
    root_test: str
    columns: list[str]   # CSV columns; the first is the storm id
    season_storms: Callable[[Engine, int, datetime], list[dict]]
    active_pairs: Callable[[Engine, list[str], datetime], set[tuple[str, str]]]
    near_miss_iso3s: Callable[[Engine, str, datetime, datetime], list[str]]


_EXPORTS = {
    NHC.key: _Export(
        BLOB_ROOT, BLOB_ROOT_TEST, HDX_CSV_COLS,
        fetch_season_storms, fetch_active_pairs, fetch_near_miss_iso3s,
    ),
    SP.key: _Export(
        BLOB_ROOT_SP, BLOB_ROOT_SP_TEST, HDX_CSV_COLS_SP,
        fetch_season_storms_sp, fetch_active_pairs_sp, fetch_near_miss_iso3s_sp,
    ),
}


def _build_adm1_rows(
    atcf_id: str,
    storm_iso3s: set[str],
    final_iso3s: set[str],
    iso3_to_name: dict[str, str],
    fm_name_by_pcode: dict[str, str],
    fcast_adm1_df: pd.DataFrame,
    obsv_adm1_df: pd.DataFrame,
    gdacs_adm1_df: pd.DataFrame,
    adam_adm1_df: pd.DataFrame,
) -> list[dict]:
    """Admin-1 rows for one storm, wide layout, MAX-combining sources per FM
    unit. The set of sources is held consistent across all admin-1 units of a
    storm-country (per wind speed) so the units are directly comparable — see
    _build_adm1_rows in pipelines/run_alert.py for the rationale."""
    f1, o1, g1, a1 = fcast_adm1_df, obsv_adm1_df, gdacs_adm1_df, adam_adm1_df

    def _match(df, iso3, pcode, wsp):
        if df.empty:
            return None
        sub = df[
            (df["iso3"] == iso3)
            & (df["fm_pcode"] == pcode)
            & (df["wind_speed_kt"] == wsp)
        ]
        return sub if not sub.empty else None

    def _num(df, iso3, pcode, wsp):
        sub = _match(df, iso3, pcode, wsp)
        return int(sub["pop_exposed"].iloc[0]) if sub is not None else 0

    def _caveat(df, iso3, pcode, wsp):
        sub = _match(df, iso3, pcode, wsp)
        if sub is None:
            return None
        val = sub["caveat_note"].iloc[0]
        return val if pd.notna(val) else None

    adm1_keys: set[tuple[str, str]] = set()
    for src in (f1, o1, g1, a1):
        sub = src[src["iso3"].isin(storm_iso3s)]
        adm1_keys |= {
            (r.iso3, r.fm_pcode) for r in sub.itertuples() if pd.notna(r.fm_pcode)
        }

    unit_vals: dict[tuple[str, str, int], dict[str, int]] = {}
    sources_used: dict[tuple[str, int], set[str]] = {}
    for iso3, pcode in adm1_keys:
        for wsp in _WIND_SPEEDS_KT:
            vals = {
                "our": _num(f1, iso3, pcode, wsp) + _num(o1, iso3, pcode, wsp),
                "ADAM": _num(a1, iso3, pcode, wsp),
                "GDACS": _num(g1, iso3, pcode, wsp),
            }
            unit_vals[(iso3, pcode, wsp)] = vals
            used = sources_used.setdefault((iso3, wsp), set())
            used |= {k for k, v in vals.items() if v > 0}

    out: list[dict] = []
    for iso3, pcode in sorted(adm1_keys):
        row: dict = {
            "atcf_id": atcf_id,
            "admin_level": 1,
            "iso3": iso3,
            "country_name": iso3_to_name.get(iso3, iso3),
            "admin_pcode": pcode,
            "admin_name": fm_name_by_pcode.get(pcode, pcode),
            "is_final_alert": iso3 in final_iso3s,
        }
        any_value = False
        for wsp in _WIND_SPEEDS_KT:
            vals = unit_vals[(iso3, pcode, wsp)]
            used = sources_used.get((iso3, wsp), set())
            ordered = [k for k in ("our", "ADAM", "GDACS") if k in used]
            unit_val = max(vals[k] for k in ordered) if ordered else 0
            row[f"pop_exposed_{wsp}kt"] = unit_val
            row[f"sources_{wsp}kt"] = (
                "|".join(_SRC_LABELS[k] for k in ordered) if unit_val > 0 else ""
            )
            cavs = [
                c
                for c in (_caveat(g1, iso3, pcode, wsp), _caveat(a1, iso3, pcode, wsp))
                if c
            ]
            row[f"caveat_{wsp}kt"] = " | ".join(dict.fromkeys(cavs))
            any_value = any_value or any(v > 0 for v in vals.values())
        if any_value:
            out.append(row)
    return out


def build_storm_rows(
    engine: Engine,
    atcf_id: str,
    issued_time: datetime,
    active_iso3s: set[str],
    country_names: "CountryNames",
    keep_iso3s: list[str] | None = None,
    basin: BasinProfile = NHC,
) -> pd.DataFrame:
    """Admin-0 + admin-1 rows for one storm at its latest advisory
    `issued_time`, in HDX_CSV_COLS order. Empty if the storm has no positive
    exposure anywhere and no `keep_iso3s` (the scraper publishes nothing for
    it then).

    Countries: positive forecast exposure at `issued_time`, positive observed
    exposure up to it, and `keep_iso3s` (a near miss's last-threatened
    countries, reported at whatever the sources still show — zero from CHD).
    `active_iso3s` are the storm's countries still under threat at the
    current advisory; every other country is final.

    Figures are read through `basin`'s data module, and the id column is
    named as that basin's CSV names it (see _EXPORTS).
    """
    D = basin.data
    ids = [atcf_id]
    fcast_df = D.fetch_fcast_exposure(engine, issued_time)
    fcast_df = fcast_df[fcast_df["atcf_id"] == atcf_id]
    obsv_df = D.fetch_current_obsv_exposure(engine, ids, issued_time)

    storm_iso3s = sorted(
        set(fcast_df["iso3"])
        | set(obsv_df.loc[obsv_df["pop_exposed"] > 0, "iso3"])
        | set(keep_iso3s or ())
    )
    cols = _EXPORTS[basin.key].columns
    if not storm_iso3s:
        return pd.DataFrame(columns=cols)
    final_iso3s = set(storm_iso3s) - active_iso3s
    iso3_to_name = country_names.get(storm_iso3s)

    gdacs_df = D.fetch_gdacs_current_exposure(engine, ids, issued_time)
    adam_df = D.fetch_adam_current_exposure(engine, ids, issued_time)

    def _val(df: pd.DataFrame, iso3: str, wsp: int) -> int:
        sub = df[(df["iso3"] == iso3) & (df["wind_speed_kt"] == wsp)]
        return int(sub["pop_exposed"].sum()) if not sub.empty else 0

    rows: list[dict] = []
    for iso3 in storm_iso3s:
        row: dict = {
            "atcf_id": atcf_id,
            "admin_level": 0,
            "iso3": iso3,
            "country_name": iso3_to_name.get(iso3, iso3),
            "admin_pcode": "",
            "admin_name": iso3_to_name.get(iso3, iso3),
            "is_final_alert": iso3 in final_iso3s,
        }
        for wsp in _WIND_SPEEDS_KT:
            # MAX across sources, not a sum or mean (bias to action — see the
            # alert ToC loop). Each frame holds one row per (iso3, wsp).
            active = {
                k: v
                for k, v in {
                    "our": _val(fcast_df, iso3, wsp) + _val(obsv_df, iso3, wsp),
                    "ADAM": _val(adam_df, iso3, wsp),
                    "GDACS": _val(gdacs_df, iso3, wsp),
                }.items()
                if v > 0
            }
            row[f"pop_exposed_{wsp}kt"] = max(active.values()) if active else 0
            row[f"sources_{wsp}kt"] = "|".join(_SRC_LABELS[k] for k in active)
            row[f"caveat_{wsp}kt"] = ""
        rows.append(row)

    fcast_adm1_df = D.fetch_fcast_exposure_adm1(engine, issued_time)
    fcast_adm1_df = fcast_adm1_df[fcast_adm1_df["atcf_id"] == atcf_id]
    obsv_adm1_df = D.fetch_current_obsv_exposure_adm1(engine, ids, issued_time)
    gdacs_adm1_df = D.fetch_gdacs_current_exposure_adm1(engine, ids, issued_time)
    adam_adm1_df = D.fetch_adam_current_exposure_adm1(engine, ids, issued_time)

    # GDACS adm1 units with no FieldMaps match arrive as fm_pcode=NaN
    # "orphan" rows; log them, then drop them (they can't be combined with the
    # other sources by FM unit). Widest band per unit, not a sum across bands:
    # the bands nest. (ADAM orphans are dropped by the fetcher.)
    orphans = gdacs_adm1_df[gdacs_adm1_df["fm_pcode"].isna()]
    if not orphans.empty:
        logger.warning(
            "%s: dropping %d GDACS adm1 unit(s) with no FieldMaps match "
            "(~%d pop in the widest wind band).",
            atcf_id,
            orphans["gdacs_admins"].nunique(),
            int(orphans.groupby("gdacs_admins")["pop_exposed"].max().sum()),
        )
    gdacs_adm1_df = gdacs_adm1_df[gdacs_adm1_df["fm_pcode"].notna()]

    rows.extend(
        _build_adm1_rows(
            atcf_id,
            set(storm_iso3s),
            final_iso3s,
            iso3_to_name,
            D.fetch_fm_names(engine, storm_iso3s),
            fcast_adm1_df,
            obsv_adm1_df,
            gdacs_adm1_df,
            adam_adm1_df,
        )
    )
    df = pd.DataFrame(rows).rename(columns={"atcf_id": cols[0]}).reindex(columns=cols)
    return df.sort_values(
        ["iso3", "admin_level", "admin_pcode"], na_position="first"
    ).reset_index(drop=True)


class CountryNames:
    """iso3 -> country name, the same names the alert emails and workbooks
    use: FieldMaps' modal adm0_name per iso3 (not the first row's — FieldMaps
    names some outlying units separately, e.g. JAM's "Pedro Bank (Jam.)").
    Boundaries are loaded once per country across all storms of a run."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def get(self, iso3s: list[str]) -> dict[str, str]:
        missing = sorted(set(iso3s) - set(self._names))
        if missing:
            adm1 = load_adm1_boundaries(missing)
            self._names.update(
                adm1.groupby("iso_3")["adm0_name"]
                .agg(lambda s: s.mode().iat[0])
                .to_dict()
            )
        return {i: self._names.get(i, i) for i in iso3s}


def _manifest_as_of(blob: str) -> datetime | None:
    """as_of of the manifest at `blob`, or None if there is none yet."""
    try:
        manifest = json.loads(stratus.load_blob_data(blob, stage=BLOB_STAGE))
    except ResourceNotFoundError:
        return None
    return datetime.fromisoformat(manifest["as_of"])


def export_season(
    engine: Engine,
    as_of: datetime,
    *,
    db_stage: str,
    test: bool = False,
    dry_run: bool = False,
    basin: BasinProfile = NHC,
) -> dict:
    """Build every current-season storm's CSV at `as_of` and park them on
    blob with a manifest (see the module docstring). Returns the manifest.

    `basin` picks the storms (NHC by default, or the South Pacific), the
    root they are written under and the name of the id column.

    `dry_run` builds and logs everything but uploads nothing. A storm whose
    rows fail to build is left out of the manifest (the scraper then leaves
    its HDX dataset as it was) and the run raises once the manifest is
    written, so the job's failure email fires.
    """
    export = _EXPORTS[basin.key]
    id_col = export.columns[0]
    season = basin.season_of(as_of)
    storms = export.season_storms(engine, season, as_of)
    logger.info(
        "HDX export (%s): %d storm(s) in season %d as of %s",
        basin.key, len(storms), season, as_of,
    )
    active = export.active_pairs(engine, [s["atcf_id"] for s in storms], as_of)

    root = export.root_test if test else export.root
    folder = f"{root}/{as_of:%Y-%m-%dT%H}"
    country_names = CountryNames()
    entries: list[dict] = []
    failed: list[str] = []
    for storm in storms:
        aid = storm["atcf_id"]
        storm_active = {iso3 for a, iso3 in active if a == aid}
        try:
            df = build_storm_rows(
                engine, aid, storm["issued_time"], storm_active, country_names,
                basin=basin,
            )
            if df.empty:
                missed = export.near_miss_iso3s(engine, aid, storm["issued_time"], as_of)
                if missed:
                    logger.info(
                        "HDX export: %s has no exposure left; reporting %s at "
                        "the current figures", aid, ",".join(missed),
                    )
                    df = build_storm_rows(
                        engine,
                        aid,
                        storm["issued_time"],
                        storm_active,
                        country_names,
                        keep_iso3s=missed,
                        basin=basin,
                    )
        except Exception:
            logger.exception("HDX export: %s failed, left out of the manifest", aid)
            failed.append(aid)
            continue
        if df.empty:
            logger.info("HDX export: %s has no exposure to report, skipped", aid)
            continue
        adm0 = df[df["admin_level"] == 0]
        entry = {
            id_col: aid,
            "name": storm["name"],
            "season": int(storm["season"]),
            "issued_time": storm["issued_time"].isoformat(),
            "blob": f"{folder}/{aid.lower()}.csv",
            "n_rows": len(df),
            "iso3s": sorted(adm0["iso3"]),
            # Every country final and nothing still under threat = the storm
            # no longer threatens anyone. (A WSP-only threat has no row of
            # its own, so check the active pairs too.)
            "all_final": bool(adm0["is_final_alert"].all()) and not storm_active,
        }
        logger.info(
            "HDX export: %s (%s) at %s — %d rows, %s, all_final=%s",
            aid, storm["name"], storm["issued_time"], len(df),
            ",".join(entry["iso3s"]), entry["all_final"],
        )
        if not dry_run:
            stratus.upload_csv_to_blob(df, entry["blob"], stage=BLOB_STAGE)
        entries.append(entry)

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "season": season,
        "basin": basin.key,
        "db_stage": db_stage,
        "columns": export.columns,
        "storms": entries,
        "failed": failed,
    }
    if dry_run:
        logger.info("HDX export: dry run, nothing uploaded (would write %s)", folder)
    else:
        body = json.dumps(manifest, indent=1)
        # Dated copy first, latest/ last: a reader of latest/ only ever sees a
        # manifest whose CSVs are all already in place.
        names = [f"{folder}/manifest.json"]
        latest = f"{root}/latest/manifest.json"
        latest_as_of = _manifest_as_of(latest)
        # latest/ only moves forward. A backfill run of the job (an older
        # issued_time) would otherwise point the scraper at that advisory's
        # storms — another season's, even — and it would publish them.
        if latest_as_of is None or as_of >= latest_as_of:
            names.append(latest)
        else:
            logger.warning(
                "HDX export: latest/ is at %s, after %s; writing the dated "
                "folder only.", latest_as_of, as_of,
            )
        for name in names:
            stratus.upload_blob_data(
                body, name, stage=BLOB_STAGE, content_type="application/json"
            )
        logger.info(
            "HDX export: %d storm CSV(s) + manifest -> %s", len(entries), folder
        )
    if failed:
        raise RuntimeError(f"HDX export failed for {', '.join(failed)}")
    return manifest
