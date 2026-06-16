"""FieldMaps admin-1 matching for storm exposure (vendorable).

Maps each external source's native admin-1 units onto the canonical FieldMaps
(FM) pcode, so GDACS / ADAM / CHD exposure can be compared per subnational unit,
and attaches the unit's harmonisation caveat. This is the *matching* layer only.
It is deliberately agnostic to the three things each consumer does differently:

  * time-selection  — which valid_time(s) to read (advisory-window vs
    storm-final): the CALLER fetches the source rows however it wants and passes
    them in; this module never reads exposure tables.
  * aggregation     — MAX-across-sources (the alert) vs keep-3-side-by-side
    (the workbook): downstream of matching.
  * blanking        — suppressing a number a source can't stand behind: a
    presentation choice the consumer opts into (via BLANK_KINDS_*); the matcher
    only ATTACHES the caveat, it never drops a value.

So the same matcher serves both the retrospective workbook (this repo) and the
operational per-advisory alert (ds-storms-alerts) — they differ only in those
three downstream concerns, not in how admins are matched.

CHD/NHC needs no matching: its `pcode` already IS the FM pcode at adm1.

The matchers take plain DataFrames of *already-fetched, time-resolved* source
rows (one row per source admin per wind speed) and are pure pandas — no DB access
— so they are unit-testable without a database. Only the lookup loaders and the
two metadata helpers touch the DB.

VENDORED COPY — the upstream source of truth is
ds-storm-impact-harmonisation/artefacts/16_source_distribution/fm_matching.py.
Keep the two in sync. Self-contained (pandas + sqlalchemy.text); match_gdacs /
match_adam run on the rows this repo's advisory-window adm1 fetchers return.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import text

ADMIN_LEVEL = 1

# caveat_kind -> human-readable label. Single source of truth shared by the
# workbook's per-unit caveat column and (when vendored) the alert's caveat
# column, so the two repos describe the same situation identically.
CAVEAT_LABELS = {
    # whole-country: the source has no comparable adm1 for the country at all
    "fm_adm1_only": "source is national-only for this country (CHD only)",
    # per-unit: THIS FM unit has no source counterpart (the country may have others)
    "no_gdacs_at_adm1": "no counterpart for this FM unit (CHD only)",
    "no_adam_at_adm1": "no counterpart for this FM unit (CHD only)",
    # many source units roll up into one FM unit (a valid sum)
    "aggregating_from_gdacs": "several source units summed into this unit",
    "aggregating_from_adam": "several source units summed into this unit",
    # one coarse source polygon spread across many FM units (double-counts on sum)
    "aggregated_in_gdacs":
        "one coarse source polygon, split across units (may double-count)",
    "aggregated_in_adam":
        "one coarse source polygon, split across units (may double-count)",
}

# Caveat kinds where a source CANNOT stand behind its per-unit number — either a
# double-count (aggregated_in_*) or no comparable boundary (fm_adm1_only /
# no_*_at_adm1). A consumer that wants to suppress such numbers filters on these;
# the matcher only attaches the kind, it never blanks.
BLANK_KINDS_GDACS = frozenset(
    {"aggregated_in_gdacs", "fm_adm1_only", "no_gdacs_at_adm1"})
BLANK_KINDS_ADAM = frozenset(
    {"aggregated_in_adam", "fm_adm1_only", "no_adam_at_adm1"})

_OUT_COLS = ["atcf_id", "iso3", "fm_pcode", "wind_speed_kt", "pop_exposed",
             "n_src_admins", "src_admins", "caveat_kind", "caveat_note"]

# Required input columns per matcher — the executable half of the input
# contract. Checked up front so a vendored consumer whose fetcher names a column
# differently fails LOUDLY here, rather than silently merging to all-NA and
# reporting 100% orphans (the dangerous cross-repo failure mode).
_REQ_GDACS = ("atcf_id", "iso3", "gdacs_admin_code", "admin_name",
              "wind_speed_kt", "pop_exposed")
_REQ_ADAM = ("atcf_id", "iso3", "admin_name", "wind_speed_kt", "pop_exposed")


def _require(df: pd.DataFrame, cols, who: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"{who}: missing required column(s) {missing}; "
            f"got {list(df.columns)}")


def label_caveat(kind) -> str | None:
    """caveat_kind -> human label (None passes through)."""
    if kind is None or (isinstance(kind, float) and pd.isna(kind)):
        return None
    return CAVEAT_LABELS.get(kind, kind)


# ── lookup loaders (the only exposure-agnostic DB reads) ─────────────────

def load_gdacs_lookup(engine, admin_level: int = ADMIN_LEVEL) -> pd.DataFrame:
    """The static GDACS→FM crosswalk at `admin_level`. Columns: iso3,
    gmi_admin, fm_pcode, fm_name, caveat_kind, caveat_note. Country-only-coverage
    countries carry a gmi_admin IS NULL row (used to exclude them from adm1)."""
    return pd.read_sql(text("""
        SELECT iso3, gmi_admin, fm_pcode, fm_name, caveat_kind, caveat_note
        FROM storms.gdacs_fm_lookup WHERE admin_level = :lvl
    """), engine, params={"lvl": admin_level})


def load_adam_lookup(engine, admin_level: int = ADMIN_LEVEL) -> pd.DataFrame:
    """The static ADAM→FM crosswalk at `admin_level`. Columns: iso3,
    adam_admin_name, fm_pcode, fm_name, caveat_kind, caveat_note."""
    return pd.read_sql(text("""
        SELECT iso3, adam_admin_name, fm_pcode, fm_name, caveat_kind, caveat_note
        FROM storms.adam_fm_lookup WHERE admin_level = :lvl
    """), engine, params={"lvl": admin_level})


# ── matchers (pure pandas; no DB) ────────────────────────────────────────

def match_gdacs(gdacs_rows: pd.DataFrame, lookup: pd.DataFrame) -> pd.DataFrame:
    """Map GDACS adm1 rows onto FM pcodes, SUM-aggregated per FM unit.

    gdacs_rows: one row per (atcf_id, iso3, gdacs_admin_code, wind_speed_kt)
      with `admin_name` and `pop_exposed`, ALREADY time-resolved by the caller
      (e.g. the latest snapshot within its window).
    lookup: load_gdacs_lookup(engine).

    Countries GDACS only covers nationally (a gmi_admin IS NULL row in the
    lookup) are excluded from adm1. GDACS admins with no FM match are RETURNED as
    orphan rows (fm_pcode = NA, one per admin) for the caller to log/drop —
    never silently truncated. See `_OUT_COLS` for the returned schema.
    """
    if gdacs_rows.empty:
        return pd.DataFrame(columns=_OUT_COLS)
    _require(gdacs_rows, _REQ_GDACS, "match_gdacs")
    # NOTE: country-only-coverage countries are dropped here (no orphan trail).
    # The caller must carry their national totals at adm0 — a consumer that
    # fetches adm1 ONLY would silently lose them.
    country_only = set(lookup.loc[lookup["gmi_admin"].isna(), "iso3"])
    rows = gdacs_rows[~gdacs_rows["iso3"].isin(country_only)]
    lk = lookup.loc[lookup["gmi_admin"].notna(),
                    ["iso3", "gmi_admin", "fm_pcode", "caveat_kind", "caveat_note"]]
    merged = rows.merge(lk, how="left",
                        left_on=["iso3", "gdacs_admin_code"],
                        right_on=["iso3", "gmi_admin"])
    # GDACS's source-admin identity is its code; the name is just a label.
    return _aggregate_to_fm(merged.assign(_src_id=merged["gdacs_admin_code"]))


def match_adam(adam_rows: pd.DataFrame, lookup: pd.DataFrame) -> pd.DataFrame:
    """Map ADAM adm1 rows onto FM pcodes, SUM-aggregated per FM unit.

    adam_rows: one row per (atcf_id, iso3, admin_name, wind_speed_kt) with
      `pop_exposed`, ALREADY time-resolved by the caller. ADAM matches FM by
      case-insensitive admin name. ADAM admins with no FM match are RETURNED as
      orphan rows (fm_pcode = NA) — same contract as GDACS (the caller decides
      whether to drop them; the alert pipeline currently drops ADAM orphans).
    """
    if adam_rows.empty:
        return pd.DataFrame(columns=_OUT_COLS)
    _require(adam_rows, _REQ_ADAM, "match_adam")
    rows = adam_rows.assign(_nm=adam_rows["admin_name"].str.lower())
    lk = lookup.assign(_nm=lookup["adam_admin_name"].str.lower())[
        ["iso3", "_nm", "fm_pcode", "caveat_kind", "caveat_note"]]
    merged = rows.merge(lk, how="left", on=["iso3", "_nm"])
    # ADAM's source-admin identity IS its name.
    return _aggregate_to_fm(merged.assign(_src_id=merged["admin_name"]))


def _aggregate_to_fm(merged: pd.DataFrame) -> pd.DataFrame:
    """Shared tail of both matchers: SUM matched rows per FM unit (rolling up the
    source admin names + caveat), and keep each unmatched (orphan) row as-is with
    fm_pcode = NA."""
    out = []
    matched = merged[merged["fm_pcode"].notna()]
    if not matched.empty:
        out.append(
            matched.groupby(["atcf_id", "iso3", "fm_pcode", "wind_speed_kt"],
                            as_index=False)
            .agg(pop_exposed=("pop_exposed", "sum"),
                 n_src_admins=("_src_id", "nunique"),
                 src_admins=("admin_name", _join_names),
                 caveat_kind=("caveat_kind", _agg_first),
                 caveat_note=("caveat_note", _agg_join)))
    orphan = merged[merged["fm_pcode"].isna()]
    if not orphan.empty:
        out.append(orphan.assign(
            fm_pcode=pd.NA, n_src_admins=1,
            src_admins=orphan["admin_name"],
            caveat_kind=pd.NA, caveat_note=pd.NA,
        )[_OUT_COLS])
    if not out:
        return pd.DataFrame(columns=_OUT_COLS)
    return pd.concat(out, ignore_index=True)[_OUT_COLS]


def _join_names(s):
    return " | ".join(sorted({x for x in s if pd.notna(x)}))


def _agg_first(s):
    """The unit's structural caveat_kind (deterministic MAX of non-null kinds —
    matches the lookup builders' one-kind-per-unit contract)."""
    vals = sorted({x for x in s if pd.notna(x)})
    return vals[-1] if vals else pd.NA


def _agg_join(s):
    vals = sorted({x for x in s if pd.notna(x)})
    return " | ".join(vals) if vals else pd.NA


# ── metadata helpers (DB) ────────────────────────────────────────────────

def unit_caveats(engine, admin_level: int = ADMIN_LEVEL) -> pd.DataFrame:
    """Per FM unit, its STRUCTURAL GDACS/ADAM caveat_kind (one row per fm_pcode),
    independent of any storm. This is the consistent comparability status of a
    unit — it surfaces the full taxonomy (fm_adm1_only, no_*_at_adm1,
    aggregating, aggregated), not just the caveats a given storm's exposure join
    happens to hit. Columns: fm_pcode, gdacs_caveat, adam_caveat."""
    gd = pd.read_sql(text("""
        SELECT fm_pcode, MAX(caveat_kind) AS gdacs_caveat
        FROM storms.gdacs_fm_lookup
        WHERE admin_level = :lvl AND fm_pcode IS NOT NULL
        GROUP BY fm_pcode"""), engine, params={"lvl": admin_level})
    ad = pd.read_sql(text("""
        SELECT fm_pcode, MAX(caveat_kind) AS adam_caveat
        FROM storms.adam_fm_lookup
        WHERE admin_level = :lvl AND fm_pcode IS NOT NULL
        GROUP BY fm_pcode"""), engine, params={"lvl": admin_level})
    return gd.merge(ad, on="fm_pcode", how="outer")


def fm_names(engine, admin_level: int = ADMIN_LEVEL) -> pd.DataFrame:
    """fm_pcode -> fm_name at `admin_level`, UNIONED across both lookups (so a
    unit named in only one lookup still resolves). Works at any level (adm0 the
    pcode is the iso3 and fm_name the country name). Columns: fm_pcode, fm_name;
    one row per unit."""
    return pd.read_sql(text("""
        SELECT fm_pcode, MAX(fm_name) AS fm_name FROM (
            SELECT fm_pcode, fm_name FROM storms.gdacs_fm_lookup
            WHERE admin_level = :lvl AND fm_pcode IS NOT NULL
            UNION ALL
            SELECT fm_pcode, fm_name FROM storms.adam_fm_lookup
            WHERE admin_level = :lvl AND fm_pcode IS NOT NULL
        ) z GROUP BY fm_pcode
    """), engine, params={"lvl": admin_level})
