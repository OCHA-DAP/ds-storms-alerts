"""Per-event GDACS↔NHC exposure harmonization on FieldMaps admin units.

⚠️  STATUS — CURRENTLY UNUSED.  Nothing in this repo imports this
module yet. It was written alongside the canonical FM↔GDACS lookup as
a candidate runtime path for ``pipelines/run_alert.py`` to attach FM
pcodes when building the per-storm exposure CSV the email attaches.
We don't yet know how (or whether) it will be wired in — kept here so
the work isn't lost, but treat it as a draft until something actually
consumes it. Delete if it doesn't land within a sprint or two.

Two layers:

  1. `attach_fm_pcode(gdacs_rows, lookup) -> DataFrame`
     Pure name/admin matching. Stateless. Adds `fm_pcode`, `fm_name`,
     `caveat_note` columns to raw GDACS exposure rows. Handles the
     `aggregate_gdacs_to_fm` (1 GDACS row → 1 FM row, many GDACS share
     the same FM) and the `pre_split_boundary` (1 GDACS row → multiple
     FM rows) cases naturally via the lookup's shape.

  2. `get_event_exposure(...)` and friends
     Per-event orchestration. Uses `storms.storm_id_lookup` to resolve
     storm identity across GDACS/NHC (caller can pass either
     `gdacs_eventid` or `atcf_id`), then fetches and enriches both
     halves of the exposure data.

The canonical FM↔GDACS lookup lives in `storms.gdacs_fm_lookup`
alongside the other inputs (`storms.gdacs_exposure`,
`storms.nhc_tracks_fcastonly_exposure`, `storms.storm_id_lookup`).
Built offline by `scripts/build_canonical_lookup.py` in the sibling
`ds-storms-pipeline` repo (REPLACEs the table on each run). One
process-level cache so we don't re-fetch it on every event lookup.

Downstream concerns (email formatting, CSV serialization, etc.) live
outside this module — they consume the DataFrames it returns.
"""

from __future__ import annotations

import logging
from typing import Optional

import ocha_stratus as stratus
import pandas as pd

logger = logging.getLogger(__name__)

LOOKUP_SCHEMA = "storms"
LOOKUP_TABLE = "gdacs_fm_lookup"

# Process-level cache so callers that loop over events don't re-fetch
# the lookup for every one. Refresh by passing `force=True`.
_LOOKUP_CACHE: dict[str, pd.DataFrame] = {}


def load_canonical_lookup(
    stage: str = "dev", force: bool = False, engine=None,
) -> pd.DataFrame:
    """Load the canonical FM↔GDACS lookup from ``storms.gdacs_fm_lookup``.

    Cached per (stage) for the life of the process. Pass ``force=True``
    if you've just rebuilt the lookup and want the fresh copy.
    """
    if not force and stage in _LOOKUP_CACHE:
        return _LOOKUP_CACHE[stage]
    if engine is None:
        engine = stratus.get_engine(stage=stage)
    logger.info(
        "Loading canonical FM↔GDACS lookup from %s.%s (stage=%s)",
        LOOKUP_SCHEMA, LOOKUP_TABLE, stage,
    )
    df = pd.read_sql(
        f"SELECT * FROM {LOOKUP_SCHEMA}.{LOOKUP_TABLE}", engine,
    )
    _LOOKUP_CACHE[stage] = df
    return df


# ─────────────────────────────────────────────────────────────────────
# Inner layer: pure name/admin matching
# ─────────────────────────────────────────────────────────────────────

def attach_fm_pcode(
    gdacs_exposure_rows: pd.DataFrame,
    lookup: pd.DataFrame,
) -> pd.DataFrame:
    """Attach FieldMaps identifier(s) + caveat note to each GDACS row.

    Pure function — no I/O. Input is whatever shape `storms.gdacs_exposure`
    rows have (or a subset of them); output is the same rows with
    ``fm_pcode``, ``fm_name``, ``caveat_note`` columns appended via the
    lookup.

    Row cardinality after merge:

    - ``accept`` countries: 1 input → 1 output (FM and GDACS line up 1:1)
    - ``country_only`` countries at admin_level=1: 1 input → 1 output
      with ``fm_pcode = NaN``. Caller filters out NaN if they only want
      attached rows.
    - ``aggregate_gdacs_to_fm`` countries: 1 input → 1 output, but
      multiple GDACS rows now share the same ``fm_pcode``. To roll up,
      caller does ``groupby('fm_pcode').sum('pop_exposed')``.
    - ``needs_manual_mapping`` pre-split cases (e.g., CUB old `La Habana`
      polygon → both Artemisa and Mayabeque): **1 input → N outputs**.
      Same ``pop_exposed`` appears under each child FM unit with the
      caveat populated so downstream knows not to sum across them.
    """
    if gdacs_exposure_rows.empty:
        return gdacs_exposure_rows.assign(
            fm_pcode=pd.NA, fm_name=pd.NA, caveat_note=pd.NA,
        )
    keep_cols = [
        "iso3", "admin_level", "gmi_admin",
        "fm_pcode", "fm_name", "caveat_note",
    ]
    return gdacs_exposure_rows.merge(
        lookup[keep_cols],
        left_on=["iso3", "admin_level", "gdacs_admin_code"],
        right_on=["iso3", "admin_level", "gmi_admin"],
        how="left",
    ).drop(columns=["gmi_admin"])


# ─────────────────────────────────────────────────────────────────────
# Outer layer: per-event orchestration via storm_id_lookup
# ─────────────────────────────────────────────────────────────────────

def resolve_storm_ids(
    gdacs_eventid: Optional[int] = None,
    atcf_id: Optional[str] = None,
    engine=None,
    stage: str = "dev",
) -> dict:
    """Look up all known IDs for a storm via ``storms.storm_id_lookup``.

    Pass at least one of (``gdacs_eventid``, ``atcf_id``); returns the
    full row of cross-source IDs (``gdacs_eventid``, ``atcf_id``,
    ``sid``, ``adam_eventid``). Missing/unknown IDs come back as None.

    Defensive: if the lookup table has no row for the given input,
    returns a dict with the input echoed back and the rest None — the
    caller can still fetch whichever side it has an ID for.
    """
    if gdacs_eventid is None and atcf_id is None:
        raise ValueError("must provide gdacs_eventid or atcf_id")
    if engine is None:
        engine = stratus.get_engine(stage=stage)

    clauses = []
    params: dict = {}
    if gdacs_eventid is not None:
        clauses.append("gdacs_eventid = %(eid)s")
        params["eid"] = int(gdacs_eventid)
    if atcf_id is not None:
        clauses.append("atcf_id = %(aid)s")
        params["aid"] = str(atcf_id)
    sql = (
        "SELECT gdacs_eventid, atcf_id, sid, adam_eventid "
        "FROM storms.storm_id_lookup WHERE " + " OR ".join(clauses)
    )
    rows = pd.read_sql(sql, engine, params=params)

    if rows.empty:
        return {
            "gdacs_eventid": gdacs_eventid,
            "atcf_id": atcf_id,
            "sid": None,
            "adam_eventid": None,
        }
    r = rows.iloc[0]
    return {
        "gdacs_eventid": int(r["gdacs_eventid"])
        if pd.notna(r["gdacs_eventid"]) else None,
        "atcf_id": r["atcf_id"] if pd.notna(r["atcf_id"]) else None,
        "sid": r["sid"] if pd.notna(r["sid"]) else None,
        "adam_eventid": int(r["adam_eventid"])
        if pd.notna(r["adam_eventid"]) else None,
    }


def fetch_gdacs_exposure(gdacs_eventid: int, engine) -> pd.DataFrame:
    """All ``storms.gdacs_exposure`` rows for one event (all episodes,
    both admin levels)."""
    return pd.read_sql(
        "SELECT * FROM storms.gdacs_exposure "
        "WHERE gdacs_eventid = %(eid)s",
        engine,
        params={"eid": int(gdacs_eventid)},
    )


def fetch_nhc_exposure(atcf_id: str, engine) -> pd.DataFrame:
    """All ``storms.nhc_tracks_fcastonly_exposure`` rows for one ATCF
    storm (all advisories, both admin levels). Same table the email
    pipeline reads via ``ds-storms-alerts/src/data.fetch_fcast_exposure``,
    so the dashboard's comparison cell aligns with the email's TOC."""
    return pd.read_sql(
        "SELECT * FROM storms.nhc_tracks_fcastonly_exposure "
        "WHERE atcf_id = %(aid)s",
        engine,
        params={"aid": str(atcf_id)},
    )


def get_event_exposure(
    gdacs_eventid: Optional[int] = None,
    atcf_id: Optional[str] = None,
    stage: str = "dev",
    lookup: Optional[pd.DataFrame] = None,
) -> dict:
    """Fetch both halves of exposure for one storm, harmonized.

    Returns ``{'storm': {...IDs...}, 'gdacs_fm': DataFrame, 'nhc': DataFrame}``.

    - ``storm``: resolved IDs from ``storms.storm_id_lookup``.
    - ``gdacs_fm``: ``storms.gdacs_exposure`` rows with FM pcode + caveat
      attached via ``attach_fm_pcode`` (renamed ``pop_exposed`` →
      ``gdacs_pop_exposed`` for clarity). Empty if the storm has no
      GDACS exposure on file.
    - ``nhc``: ``storms.nhc_tracks_fcastonly_exposure`` rows as-is
      (already FM-keyed). Empty if the storm has no NHC exposure on
      file.

    Both DataFrames share schema columns ``admin_level``, ``iso3``, and
    the FM ``pcode``/``fm_pcode`` so the caller can merge them however
    they like (e.g., latest advisory per pcode + matched GDACS episode).
    Intentionally not pre-merged here — the time-alignment policy
    (latest? per-snapshot? episode-matched?) depends on what the
    consumer wants and isn't this module's call.
    """
    engine = stratus.get_engine(stage=stage)
    storm = resolve_storm_ids(
        gdacs_eventid=gdacs_eventid, atcf_id=atcf_id, engine=engine,
    )

    if lookup is None:
        lookup = load_canonical_lookup(stage=stage)

    gdacs_fm = pd.DataFrame()
    if storm["gdacs_eventid"] is not None:
        raw = fetch_gdacs_exposure(storm["gdacs_eventid"], engine)
        if not raw.empty:
            gdacs_fm = attach_fm_pcode(raw, lookup).rename(
                columns={"pop_exposed": "gdacs_pop_exposed"}
            )

    nhc = pd.DataFrame()
    if storm["atcf_id"] is not None:
        nhc = fetch_nhc_exposure(storm["atcf_id"], engine)

    return {"storm": storm, "gdacs_fm": gdacs_fm, "nhc": nhc}
