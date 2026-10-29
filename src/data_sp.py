"""Data access for the South Pacific (RSMC Nadi / FMS) alert.

Same function names and column contracts as src/data.py, so the alert
renderer in pipelines/run_alert.py can run on either basin (see
src/basins.py). The tables are written by ds-storms-pipeline's
src/pipelines/sp.py:

  storms.fms_tracks_{fcastonly,obsv}_{buffers,exposure}  realtime, per FMS forecast
  storms.fms_tracks_scenario_{exposure,envelope}          uncertainty scenarios
  storms.ibtracs_sp_wind_exposure                         historical record
  storms.sp_admin_population                              country totals

Differences from NHC that the functions absorb:

* ``atcf_id`` columns carry the FMS disturbance id (e.g. ``2526-11F``) for
  current storms and the IBTrACS sid for historical ones.
* FMS storms do not share advisory times. "Current" at time T means each
  storm's latest Official forecast issued in (T - 12 h, T] whose exposure has
  been computed; "previous" is that storm's forecast before it.
* There are no wind-speed probabilities and no GDACS/ADAM figures (v1): those
  fetchers return empty frames with the NHC columns.
* Winds are FMS 10-minute means.
"""

from datetime import UTC, datetime, timedelta

import geopandas as gpd
import pandas as pd
from sqlalchemy import Engine, bindparam, text

from src.data import _disk_cached, load_adm1_boundaries

OFFICIAL_TRACK = "Official Forecast Track"
CURRENT_WINDOW = timedelta(hours=12)
MAX_LEADTIME_H = 120
N_SCENARIOS = 36

# The 16 Pacific island countries and territories the South Pacific alert
# covers (FMS area of responsibility and its edges).
SP_ISO3S = [
    "ASM", "COK", "FJI", "KIR", "NCL", "NIU", "NRU", "PCN",
    "PYF", "SLB", "TKL", "TON", "TUV", "VUT", "WLF", "WSM",
]

_ADMIN_LEVEL = 0
_ADMIN_LEVEL_1 = 1


def _cached(fn):
    """src.data's disk cache, keyed apart from its same-named NHC fetchers
    (the cache key is the function name plus arguments)."""
    name = fn.__name__
    fn.__name__ = f"sp_{name}"
    wrapper = _disk_cached(fn)
    wrapper.__name__ = name
    return wrapper

# Latest processed Official forecast per storm in (t - window, t].
_CURRENT_CTE = """
    cur AS (
        SELECT f.disturbance_id, MAX(f.issued_time) AS it
        FROM storms.fms_forecasts f
        JOIN storms.fms_processing_completion c
          ON c.disturbance_id = f.disturbance_id
         AND c.issued_time = f.issued_time
         AND c.stage = 'exposure'
        WHERE f.track_type = :official
          AND f.issued_time <= :t
          AND f.issued_time > :t_min
        GROUP BY f.disturbance_id
    )
"""

_STORM_COLS = """
    s.name AS name, s.season AS season
"""


def _params(t: datetime, **extra) -> dict:
    return {"t": t, "t_min": t - CURRENT_WINDOW, "official": OFFICIAL_TRACK, **extra}


def _df(engine: Engine, sql, params: dict, cols: list[str] | None = None) -> pd.DataFrame:
    with engine.connect() as conn:
        result = conn.execute(sql, params)
        df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    return df if cols is None else df.reindex(columns=cols)


def season_of(t: datetime) -> int:
    """Southern Hemisphere season, named by the year it ends (Jul 2026 -> 2027)."""
    return t.year + 1 if t.month >= 7 else t.year


def most_recent_advisory_time(engine: Engine) -> datetime:
    """Latest processed FMS Official forecast time (UTC, naive); now if none."""
    sql = text("""
        SELECT MAX(f.issued_time) FROM storms.fms_forecasts f
        JOIN storms.fms_processing_completion c
          ON c.disturbance_id = f.disturbance_id AND c.issued_time = f.issued_time
         AND c.stage = 'exposure'
        WHERE f.track_type = :official
    """)
    with engine.connect() as conn:
        t = conn.execute(sql, {"official": OFFICIAL_TRACK}).scalar()
    return t or datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)


@_cached
def fetch_current_issuances(engine: Engine, issued_time: datetime) -> dict[str, datetime]:
    """{disturbance_id: issued_time of its current forecast} at ``issued_time``."""
    sql = text(f"WITH {_CURRENT_CTE} SELECT disturbance_id, it FROM cur")
    df = _df(engine, sql, _params(issued_time))
    return dict(zip(df.disturbance_id, df.it))


@_cached
def fetch_fcast_exposure(engine: Engine, issued_time: datetime) -> pd.DataFrame:
    """Forecast-only exposure at each storm's current forecast (admin 0, > 0).

    Returns columns: atcf_id, iso3, wind_speed_kt, pop_exposed, name, season.
    """
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT e.disturbance_id AS atcf_id, e.iso3, e.wind_speed_kt, e.pop_exposed,
               {_STORM_COLS}
        FROM storms.fms_tracks_fcastonly_exposure e
        JOIN cur ON cur.disturbance_id = e.disturbance_id AND cur.it = e.issued_time
        JOIN storms.fms_storms s ON s.disturbance_id = e.disturbance_id
        WHERE e.admin_level = :al AND e.pop_exposed > 0
    """)
    return _df(engine, sql, _params(issued_time, al=_ADMIN_LEVEL),
               ["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"])


@_cached
def fetch_fcast_exposure_adm1(engine: Engine, issued_time: datetime) -> pd.DataFrame:
    """Returns columns: atcf_id, iso3, fm_pcode, wind_speed_kt, pop_exposed."""
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT e.disturbance_id AS atcf_id, e.iso3, e.pcode AS fm_pcode,
               e.wind_speed_kt, e.pop_exposed
        FROM storms.fms_tracks_fcastonly_exposure e
        JOIN cur ON cur.disturbance_id = e.disturbance_id AND cur.it = e.issued_time
        WHERE e.admin_level = :al AND e.pop_exposed > 0
    """)
    return _df(engine, sql, _params(issued_time, al=_ADMIN_LEVEL_1),
               ["atcf_id", "iso3", "fm_pcode", "wind_speed_kt", "pop_exposed"])


def _obsv(engine, atcf_ids, issued_time, admin_level, cols, pcode_sel) -> pd.DataFrame:
    if not atcf_ids:
        return pd.DataFrame(columns=cols)
    sql = text(f"""
        SELECT DISTINCT ON (e.disturbance_id, e.iso3, e.pcode, e.wind_speed_kt)
               e.disturbance_id AS atcf_id, e.iso3, {pcode_sel} e.wind_speed_kt,
               e.pop_exposed, s.name AS name, s.season AS season
        FROM storms.fms_tracks_obsv_exposure e
        JOIN storms.fms_storms s ON s.disturbance_id = e.disturbance_id
        WHERE e.disturbance_id IN :ids AND e.admin_level = :al
          AND e.valid_time <= :t
        ORDER BY e.disturbance_id, e.iso3, e.pcode, e.wind_speed_kt, e.valid_time DESC
    """).bindparams(bindparam("ids", expanding=True))
    return _df(engine, sql, {"ids": list(atcf_ids), "al": admin_level, "t": issued_time}, cols)


@_cached
def fetch_current_obsv_exposure(engine: Engine, atcf_ids: list[str], issued_time: datetime) -> pd.DataFrame:
    """Latest cumulative observed exposure per (storm, country, wind).

    Returns columns: atcf_id, iso3, wind_speed_kt, pop_exposed, name, season.
    """
    return _obsv(engine, atcf_ids, issued_time, _ADMIN_LEVEL,
                 ["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"], "")


@_cached
def fetch_current_obsv_exposure_adm1(engine: Engine, atcf_ids: list[str], issued_time: datetime) -> pd.DataFrame:
    """Returns columns: atcf_id, iso3, fm_pcode, wind_speed_kt, pop_exposed."""
    return _obsv(engine, atcf_ids, issued_time, _ADMIN_LEVEL_1,
                 ["atcf_id", "iso3", "fm_pcode", "wind_speed_kt", "pop_exposed"],
                 "e.pcode AS fm_pcode,")


def _pairs_at(engine: Engine, issuances: dict) -> list[dict]:
    """(storm, country) pairs with forecast or shifted-track exposure at the
    given (storm -> issued_time) forecasts."""
    if not issuances:
        return []
    keys = list(issuances.items())
    values = ", ".join(f"(:d{i}, CAST(:t{i} AS timestamp))" for i in range(len(keys)))
    params = {"al": _ADMIN_LEVEL}
    for i, (d, t) in enumerate(keys):
        params[f"d{i}"], params[f"t{i}"] = d, t
    sql = text(f"""
        WITH k(disturbance_id, it) AS (VALUES {values})
        SELECT DISTINCT p.disturbance_id, p.iso3, s.name, s.season FROM (
            SELECT e.disturbance_id, e.iso3 FROM storms.fms_tracks_fcastonly_exposure e
            JOIN k ON k.disturbance_id = e.disturbance_id AND k.it = e.issued_time
            WHERE e.admin_level = :al AND e.pop_exposed > 0
            UNION
            SELECT e.disturbance_id, e.iso3 FROM storms.fms_tracks_scenario_exposure e
            JOIN k ON k.disturbance_id = e.disturbance_id AND k.it = e.issued_time
            WHERE e.admin_level = :al AND e.pop_exposed > 0
        ) p
        JOIN storms.fms_storms s ON s.disturbance_id = p.disturbance_id
    """)
    df = _df(engine, sql, params)
    return [
        {"atcf_id": r.disturbance_id, "iso3": r.iso3, "name": r.name, "season": r.season}
        for r in df.itertuples()
    ]


@_cached
def fetch_prev_any_pairs(
    engine: Engine, issued_time: datetime, prev_hours: int = 24,
    prev_issuances: dict | None = None,
) -> list[dict]:
    """(storm, country) pairs that had exposure in the PREVIOUS alert, so a
    pair that drops out gets its final notice exactly once.

    ``prev_issuances``: the forecasts the last email actually sent was built
    from ({storm: issued_time}, read from its campaign name by the caller).
    A storm in it that is no longer current (no forecast in the last 12 h)
    has ended, and all its pairs come back here, so they go final too. Without
    it (previews, no Listmonk), each current storm's own previous forecast
    stands in for the previous alert.
    """
    if prev_issuances is not None:
        return _pairs_at(engine, prev_issuances)
    sql = text(f"""
        WITH {_CURRENT_CTE},
        prev AS (
            SELECT f.disturbance_id, MAX(f.issued_time) AS it
            FROM storms.fms_forecasts f
            JOIN cur ON cur.disturbance_id = f.disturbance_id
            JOIN storms.fms_processing_completion c
              ON c.disturbance_id = f.disturbance_id
             AND c.issued_time = f.issued_time AND c.stage = 'exposure'
            WHERE f.track_type = :official
              AND f.issued_time < cur.it
              AND f.issued_time >= cur.it - make_interval(hours => :prev_hours)
            GROUP BY f.disturbance_id
        )
        SELECT DISTINCT p.disturbance_id, p.iso3, s.name, s.season FROM (
            SELECT e.disturbance_id, e.iso3 FROM storms.fms_tracks_fcastonly_exposure e
            JOIN prev ON prev.disturbance_id = e.disturbance_id AND prev.it = e.issued_time
            WHERE e.admin_level = :al AND e.pop_exposed > 0
            UNION
            -- a country only the shifted tracks reached was alerted too, so
            -- it needs its final notice as well
            SELECT e.disturbance_id, e.iso3 FROM storms.fms_tracks_scenario_exposure e
            JOIN prev ON prev.disturbance_id = e.disturbance_id AND prev.it = e.issued_time
            WHERE e.admin_level = :al AND e.pop_exposed > 0
        ) p
        JOIN storms.fms_storms s ON s.disturbance_id = p.disturbance_id
    """)
    df = _df(engine, sql, _params(issued_time, al=_ADMIN_LEVEL, prev_hours=prev_hours))
    return [
        {"atcf_id": r.disturbance_id, "iso3": r.iso3, "name": r.name, "season": r.season}
        for r in df.itertuples()
    ]


@_cached
def fetch_all_prior_country_pairs(engine: Engine, atcf_ids: list[str], issued_time: datetime) -> dict:
    """{(storm, iso3): last issued_time} for pairs with forecast exposure at any
    forecast before each storm's current one."""
    if not atcf_ids:
        return {}
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT e.disturbance_id, e.iso3, MAX(e.issued_time) AS last_t FROM (
            SELECT disturbance_id, iso3, issued_time, pop_exposed, admin_level
            FROM storms.fms_tracks_fcastonly_exposure
            UNION ALL
            SELECT disturbance_id, iso3, issued_time, pop_exposed, admin_level
            FROM storms.fms_tracks_scenario_exposure
        ) e
        JOIN cur ON cur.disturbance_id = e.disturbance_id
        WHERE e.disturbance_id IN :ids AND e.admin_level = :al
          AND e.issued_time < cur.it AND e.pop_exposed > 0
        GROUP BY e.disturbance_id, e.iso3
    """).bindparams(bindparam("ids", expanding=True))
    df = _df(engine, sql, _params(issued_time, ids=list(atcf_ids), al=_ADMIN_LEVEL))
    return {(r.disturbance_id, r.iso3): r.last_t for r in df.itertuples()}


def _ibtracs_sids_for(engine: Engine, disturbance_ids: list[str]) -> list[str]:
    """IBTrACS sids of the given FMS storms, so a current storm is not compared
    with itself: same name and season, or (unnamed storms, renamed records) an
    IBTrACS position within 300 km of an FMS analysis position at the same time."""
    if not disturbance_ids:
        return []
    sql = text("""
        SELECT DISTINCT ib.sid FROM storms.fms_storms s
        JOIN storms.ibtracs_storms ib
          ON upper(ib.name) = upper(s.name) AND ib.season = s.season
        WHERE s.disturbance_id IN :ids AND s.name IS NOT NULL
        UNION
        SELECT DISTINCT ib.sid FROM storms.fms_tracks_geo f
        JOIN storms.ibtracs_tracks_geo ib
          ON ib.valid_time = f.valid_time
         AND ST_DWithin(ib.geometry::geography, f.geometry::geography, 300000)
        WHERE f.disturbance_id IN :ids AND f.leadtime = 0
    """).bindparams(bindparam("ids", expanding=True))
    return _df(engine, sql, {"ids": list(disturbance_ids)})["sid"].tolist()


@_cached
def fetch_historical_obsv_exposure(engine: Engine, iso3s: list[str], exclude_atcf_ids: list[str]) -> pd.DataFrame:
    """Whole-track exposure per past South Pacific storm (IBTrACS, season 2002+).

    Returns columns: atcf_id (IBTrACS sid), iso3, wind_speed_kt, pop_exposed, name, season.
    """
    cols = ["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"]
    if not iso3s:
        return pd.DataFrame(columns=cols)
    exclude = set(exclude_atcf_ids) | set(_ibtracs_sids_for(engine, exclude_atcf_ids))
    sql = text("""
        SELECT e.sid AS atcf_id, e.iso3, e.wind_speed_kt, e.pop_exposed,
               COALESCE(NULLIF(ib.name, 'NOT_NAMED'), 'Unnamed') AS name, ib.season
        FROM storms.ibtracs_sp_wind_exposure e
        JOIN storms.ibtracs_storms ib ON ib.sid = e.sid
        WHERE e.iso3 IN :iso3s AND e.admin_level = :al
    """).bindparams(bindparam("iso3s", expanding=True))
    df = _df(engine, sql, {"iso3s": list(iso3s), "al": _ADMIN_LEVEL}, cols)
    return df[~df["atcf_id"].isin(exclude)].reset_index(drop=True)


@_cached
def fetch_admin_population(engine: Engine, iso3s: list[str]) -> dict[str, int]:
    """{iso3: total population} on the same sea-extended units as the exposure."""
    if not iso3s:
        return {}
    sql = text("""
        SELECT iso3, total_pop FROM storms.sp_admin_population
        WHERE admin_level = 0 AND iso3 IN :iso3s
    """).bindparams(bindparam("iso3s", expanding=True))
    df = _df(engine, sql, {"iso3s": list(iso3s)})
    return {r.iso3: int(r.total_pop) for r in df.itertuples()}


@_cached
def fetch_active_storm_meta(engine: Engine, issued_time: datetime) -> list[dict]:
    """Storms with a current forecast. Returns [{atcf_id, name, season}]."""
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT cur.disturbance_id, s.name, s.season FROM cur
        JOIN storms.fms_storms s ON s.disturbance_id = cur.disturbance_id
        ORDER BY cur.disturbance_id
    """)
    df = _df(engine, sql, _params(issued_time))
    return [{"atcf_id": r.disturbance_id, "name": r.name, "season": r.season} for r in df.itertuples()]


def fetch_all_monitored_countries(engine: Engine) -> list[str]:
    """Countries that get a sign-up list. Not Kiribati: WorldPop 2026 has no
    population there, so no Kiribati alert can fire until another source is
    added (its boundaries still draw on the maps)."""
    return [c for c in SP_ISO3S if c != "KIR"]


@_cached
def fetch_track_geo(engine: Engine, atcf_ids: list[str], issued_time: datetime) -> gpd.GeoDataFrame:
    """Observed track (analysis points of every forecast up to T) and the
    current forecast's points (to 120 h).

    Returns columns: atcf_id, valid_time, wind_speed, kind, geometry.
    """
    cols = ["atcf_id", "valid_time", "wind_speed", "kind", "geometry"]
    if not atcf_ids:
        return gpd.GeoDataFrame(columns=cols, crs="EPSG:4326")
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT atcf_id, valid_time, wind_speed, kind, geometry FROM (
            SELECT DISTINCT ON (g.disturbance_id, g.valid_time)
                   g.disturbance_id AS atcf_id, g.valid_time, g.wind_speed,
                   'observed' AS kind, g.geometry
            FROM storms.fms_tracks_geo g
            WHERE g.disturbance_id IN :ids AND g.track_type = :official
              AND g.leadtime = 0 AND g.issued_time <= :t
            ORDER BY g.disturbance_id, g.valid_time, g.issued_time DESC
        ) o
        UNION ALL
        SELECT g.disturbance_id AS atcf_id, g.valid_time, g.wind_speed,
               'forecast' AS kind, g.geometry
        FROM storms.fms_tracks_geo g
        JOIN cur ON cur.disturbance_id = g.disturbance_id AND cur.it = g.issued_time
        WHERE g.disturbance_id IN :ids AND g.track_type = :official
          AND g.leadtime > 0 AND g.leadtime <= :maxlt
    """).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        gdf = gpd.read_postgis(
            sql, conn, geom_col="geometry",
            params=_params(issued_time, ids=list(atcf_ids), maxlt=MAX_LEADTIME_H),
        )
    return gdf[cols]


@_cached
def fetch_buffers(engine: Engine, atcf_ids: list[str], issued_time: datetime) -> gpd.GeoDataFrame:
    """Latest observed swath and the current forecast-only swath, per wind.

    Returns columns: atcf_id, wind_speed_kt, kind, geometry.
    """
    cols = ["atcf_id", "wind_speed_kt", "kind", "geometry"]
    if not atcf_ids:
        return gpd.GeoDataFrame(columns=cols, crs="EPSG:4326")
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT atcf_id, wind_speed_kt, kind, geometry FROM (
            SELECT DISTINCT ON (b.disturbance_id, b.wind_speed_kt)
                   b.disturbance_id AS atcf_id, b.wind_speed_kt, 'observed' AS kind, b.geometry
            FROM storms.fms_tracks_obsv_buffers b
            WHERE b.disturbance_id IN :ids AND b.valid_time <= :t
            ORDER BY b.disturbance_id, b.wind_speed_kt, b.valid_time DESC
        ) o
        UNION ALL
        SELECT b.disturbance_id AS atcf_id, b.wind_speed_kt, 'forecast' AS kind, b.geometry
        FROM storms.fms_tracks_fcastonly_buffers b
        JOIN cur ON cur.disturbance_id = b.disturbance_id AND cur.it = b.issued_time
        WHERE b.disturbance_id IN :ids
    """).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        gdf = gpd.read_postgis(sql, conn, geom_col="geometry",
                               params=_params(issued_time, ids=list(atcf_ids)))
    return gdf[cols]


@_cached
def fetch_scenario_exposure(engine: Engine, atcf_ids: list[str], issued_time: datetime) -> pd.DataFrame:
    """Per (storm, country, wind): the highest forecast-only exposure across the
    uncertainty scenarios, and how many of them expose anyone at all.

    Returns columns: atcf_id, iso3, wind_speed_kt, scen_max, scen_n_nonzero, n_scenarios.
    """
    cols = ["atcf_id", "iso3", "wind_speed_kt", "scen_max", "scen_n_nonzero", "n_scenarios"]
    if not atcf_ids:
        return pd.DataFrame(columns=cols)
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT e.disturbance_id AS atcf_id, e.iso3, e.wind_speed_kt,
               MAX(e.pop_exposed) AS scen_max,
               COUNT(*) FILTER (WHERE e.pop_exposed > 0) AS scen_n_nonzero,
               :n AS n_scenarios
        FROM storms.fms_tracks_scenario_exposure e
        JOIN cur ON cur.disturbance_id = e.disturbance_id AND cur.it = e.issued_time
        WHERE e.disturbance_id IN :ids AND e.admin_level = :al
        GROUP BY e.disturbance_id, e.iso3, e.wind_speed_kt
        HAVING MAX(e.pop_exposed) > 0
    """).bindparams(bindparam("ids", expanding=True))
    return _df(engine, sql, _params(issued_time, ids=list(atcf_ids), al=_ADMIN_LEVEL, n=N_SCENARIOS), cols)


@_cached
def fetch_scenario_envelope(
    engine: Engine, atcf_ids: list[str], issued_time: datetime, wind_threshold_kt: int = 34
) -> gpd.GeoDataFrame:
    """Union of the uncertainty scenarios' forecast-only swaths at one wind.

    Returns columns: atcf_id, geometry.
    """
    cols = ["atcf_id", "geometry"]
    if not atcf_ids:
        return gpd.GeoDataFrame(columns=cols, crs="EPSG:4326")
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT v.disturbance_id AS atcf_id, v.geometry
        FROM storms.fms_tracks_scenario_envelope v
        JOIN cur ON cur.disturbance_id = v.disturbance_id AND cur.it = v.issued_time
        WHERE v.disturbance_id IN :ids AND v.wind_speed_kt = :k AND v.geometry IS NOT NULL
    """).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        gdf = gpd.read_postgis(sql, conn, geom_col="geometry",
                               params=_params(issued_time, ids=list(atcf_ids), k=wind_threshold_kt))
    return gdf[cols]


@_cached
def fetch_fm_names(engine: Engine, iso3s: list[str]) -> dict[str, str]:
    """{FieldMaps adm1 pcode: name}, straight from the boundaries."""
    if not iso3s:
        return {}
    adm1 = load_adm1_boundaries(list(iso3s))
    return dict(zip(adm1["adm1_id"], adm1["adm1_name"]))


def fetch_lookup_caveats(engine: Engine, iso3s: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=["source", "iso3", "admin_level", "scope",
                                 "caveat_kind", "caveat_note", "note"])


# --- Not available for the South Pacific in v1: empty, NHC-shaped -----------

def _empty(cols):
    def f(*args, **kwargs):
        return pd.DataFrame(columns=cols)
    return f


fetch_wsp_fcastonly_exposure = _empty(["atcf_id", "iso3", "wind_threshold_kt", "percentage", "pop_exposed"])
fetch_gdacs_current_exposure = _empty(["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"])
fetch_adam_current_exposure = _empty(["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"])
fetch_gdacs_historical_exposure = _empty(["gdacs_eventid", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"])
fetch_adam_historical_exposure = _empty(["adam_eventid", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"])
fetch_gdacs_current_exposure_adm1 = _empty(["atcf_id", "iso3", "fm_pcode", "wind_speed_kt", "pop_exposed",
                                            "n_gdacs_admins", "gdacs_admins", "caveat_note"])
fetch_adam_current_exposure_adm1 = _empty(["atcf_id", "iso3", "fm_pcode", "wind_speed_kt", "pop_exposed",
                                           "n_adam_admins", "adam_admins", "caveat_note"])


def fetch_wsp_fcastonly_polygons(*args, **kwargs) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns=["atcf_id", "percentage", "geometry"], crs="EPSG:4326")
