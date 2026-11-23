"""Data access for the South-West Indian Ocean (RSMC La Réunion / Météo-France)
alert.

Same function names and column contracts as src/data.py and src/data_sp.py,
so the alert renderer in pipelines/run_alert.py runs on this basin too (see
src/basins.py). The tables are written by ds-storms-pipeline's
src/pipelines/swio.py:

  storms.mfr_forecasts, storms.mfr_storms, storms.mfr_tracks_geo   forecasts
  storms.mfr_tracks_{fcastonly,obsv}_{buffers,exposure}           per forecast
  storms.mfr_tracks_scenario_{exposure,envelope}                  uncertainty
  storms.mfr_processing_completion                                stages done
  storms.swio_hist_wind_{buffers,exposure}                        history
  storms.swio_admin_population                                    totals

Differences from the South Pacific module:

* A forecast is a row of mfr_forecasts with kind 'forecast' and forecast
  points; files Météo-France re-published more than 24 h late
  (late_reissue) are left out.
* "Current" at time T means each system's latest forecast issued in
  (T - 12 h, T] whose uncertainty scenarios are done (stage 'scenarios', the
  last stage swio.py marks), so an email never goes out half-built.
* The observed track is the analysis points (data_type 'analysis') of every
  forecast issued up to T, a later forecast's analysis of a time replacing an
  earlier one: the same points swio.py builds the observed swath from.
* Thresholds are 34, 48 and 64 kt: Météo-France gives no 50 kt radius.
* History is keyed by Météo-France disturbance_id (an IBTrACS sid only where
  Météo-France has no system) and its season column is the season's START
  year; it is returned here as the END year, like every other season here.
* Unnamed systems are called "Disturbance <number>", and current systems
  carry no season (so their headings read "Disturbance 3", not
  "Disturbance 3 2027"; the id, e.g. 03-20262027, names the season).
"""

from datetime import UTC, datetime, timedelta

import geopandas as gpd
import pandas as pd
from sqlalchemy import Engine, bindparam, text

from src.data import _disk_cached, load_adm1_boundaries
from src.data_sp import season_of  # noqa: F401  (Southern Hemisphere: end year)

CURRENT_WINDOW = timedelta(hours=12)
MAX_LEADTIME_H = 120
N_SCENARIOS = 36
WIND_SPEEDS_KT = (34, 48, 64)
# Stage swio.py marks last for a forecast: buffers, exposure, then scenarios.
_DONE_STAGE = "scenarios"

# Countries with population totals in storms.swio_admin_population: the
# islands, the African mainland the storms reach, and the basin's edges.
SWIO_ISO3S = [
    "BWA", "COM", "IOT", "KEN", "LSO", "MDG", "MDV", "MOZ", "MUS", "MWI",
    "MYT", "NAM", "REU", "SOM", "SWZ", "SYC", "TZA", "ZAF", "ZMB", "ZWE",
]

# Islands Natural Earth's 1:110m background drops: drawn from their own
# boundaries and named on the maps.
SWIO_ISLAND_NAMES = {
    "COM": "Comoros", "MYT": "Mayotte", "REU": "Réunion",
    "MUS": "Mauritius", "SYC": "Seychelles",
}

_ADMIN_LEVEL = 0
_ADMIN_LEVEL_1 = 1


def _cached(fn):
    """src.data's disk cache, keyed apart from the same-named NHC and South
    Pacific fetchers (the cache key is the function name plus arguments)."""
    name = fn.__name__
    fn.__name__ = f"swio_{name}"
    wrapper = _disk_cached(fn)
    wrapper.__name__ = name
    return wrapper


# Forecasts that count: real forecasts, not track-only files or late copies.
_IS_FORECAST = "f.kind = 'forecast' AND f.n_forecast > 0 AND NOT f.late_reissue"

# Latest fully processed forecast per system in (t - window, t].
_CURRENT_CTE = f"""
    cur AS (
        SELECT f.disturbance_id, MAX(f.issued_time) AS it
        FROM storms.mfr_forecasts f
        JOIN storms.mfr_processing_completion c
          ON c.disturbance_id = f.disturbance_id
         AND c.issued_time = f.issued_time
         AND c.stage = :done
        WHERE {_IS_FORECAST}
          AND f.issued_time <= :t
          AND f.issued_time > :t_min
        GROUP BY f.disturbance_id
    )
"""

_NAME = "COALESCE(s.name, 'Disturbance ' || s.number::text)"


def _params(t: datetime, **extra) -> dict:
    return {"t": t, "t_min": t - CURRENT_WINDOW, "done": _DONE_STAGE, **extra}


def _df(engine: Engine, sql, params: dict, cols: list[str] | None = None) -> pd.DataFrame:
    with engine.connect() as conn:
        result = conn.execute(sql, params)
        df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    return df if cols is None else df.reindex(columns=cols)


def most_recent_advisory_time(engine: Engine) -> datetime:
    """Latest fully processed Météo-France forecast time (UTC, naive); now if none."""
    sql = text(f"""
        SELECT MAX(f.issued_time) FROM storms.mfr_forecasts f
        JOIN storms.mfr_processing_completion c
          ON c.disturbance_id = f.disturbance_id AND c.issued_time = f.issued_time
         AND c.stage = :done
        WHERE {_IS_FORECAST}
    """)
    with engine.connect() as conn:
        t = conn.execute(sql, {"done": _DONE_STAGE}).scalar()
    return t or datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)


@_cached
def fetch_current_issuances(engine: Engine, issued_time: datetime) -> dict[str, datetime]:
    """{disturbance_id: issued_time of its current forecast} at ``issued_time``."""
    sql = text(f"WITH {_CURRENT_CTE} SELECT disturbance_id, it FROM cur")
    df = _df(engine, sql, _params(issued_time))
    return dict(zip(df.disturbance_id, df.it))


@_cached
def fetch_fcast_exposure(engine: Engine, issued_time: datetime) -> pd.DataFrame:
    """Forecast-only exposure at each system's current forecast (admin 0, > 0).

    Returns columns: atcf_id, iso3, wind_speed_kt, pop_exposed, name, season.
    """
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT e.disturbance_id AS atcf_id, e.iso3, e.wind_speed_kt, e.pop_exposed,
               {_NAME} AS name, NULL::int AS season
        FROM storms.mfr_tracks_fcastonly_exposure e
        JOIN cur ON cur.disturbance_id = e.disturbance_id AND cur.it = e.issued_time
        JOIN storms.mfr_storms s ON s.disturbance_id = e.disturbance_id
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
        FROM storms.mfr_tracks_fcastonly_exposure e
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
               e.pop_exposed, {_NAME} AS name, NULL::int AS season
        FROM storms.mfr_tracks_obsv_exposure e
        JOIN storms.mfr_storms s ON s.disturbance_id = e.disturbance_id
        WHERE e.disturbance_id IN :ids AND e.admin_level = :al
          AND e.valid_time <= :t
        ORDER BY e.disturbance_id, e.iso3, e.pcode, e.wind_speed_kt, e.valid_time DESC
    """).bindparams(bindparam("ids", expanding=True))
    return _df(engine, sql, {"ids": list(atcf_ids), "al": admin_level, "t": issued_time}, cols)


@_cached
def fetch_current_obsv_exposure(engine: Engine, atcf_ids: list[str], issued_time: datetime) -> pd.DataFrame:
    """Latest cumulative observed exposure per (system, country, wind).

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
    """(system, country) pairs with forecast or shifted-track exposure at the
    given (system -> issued_time) forecasts."""
    if not issuances:
        return []
    keys = list(issuances.items())
    values = ", ".join(f"(:d{i}, CAST(:t{i} AS timestamp))" for i in range(len(keys)))
    params = {"al": _ADMIN_LEVEL}
    for i, (d, t) in enumerate(keys):
        params[f"d{i}"], params[f"t{i}"] = d, t
    sql = text(f"""
        WITH k(disturbance_id, it) AS (VALUES {values})
        SELECT DISTINCT p.disturbance_id, p.iso3, {_NAME} AS name, NULL::int AS season FROM (
            SELECT e.disturbance_id, e.iso3 FROM storms.mfr_tracks_fcastonly_exposure e
            JOIN k ON k.disturbance_id = e.disturbance_id AND k.it = e.issued_time
            WHERE e.admin_level = :al AND e.pop_exposed > 0
            UNION
            SELECT e.disturbance_id, e.iso3 FROM storms.mfr_tracks_scenario_exposure e
            JOIN k ON k.disturbance_id = e.disturbance_id AND k.it = e.issued_time
            WHERE e.admin_level = :al AND e.pop_exposed > 0
        ) p
        JOIN storms.mfr_storms s ON s.disturbance_id = p.disturbance_id
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
    """(system, country) pairs that had exposure in the PREVIOUS alert, so a
    pair that drops out gets its final notice exactly once.

    ``prev_issuances``: the forecasts the last email actually sent was built
    from ({system: issued_time}, read from its campaign name by the caller).
    A system in it that is no longer current (no forecast in the last 12 h)
    has ended, and all its pairs come back here, so they go final too.
    Without it (previews, no Listmonk), each current system's own previous
    forecast stands in for the previous alert.
    """
    if prev_issuances is not None:
        return _pairs_at(engine, prev_issuances)
    sql = text(f"""
        WITH {_CURRENT_CTE},
        prev AS (
            SELECT f.disturbance_id, MAX(f.issued_time) AS it
            FROM storms.mfr_forecasts f
            JOIN cur ON cur.disturbance_id = f.disturbance_id
            JOIN storms.mfr_processing_completion c
              ON c.disturbance_id = f.disturbance_id
             AND c.issued_time = f.issued_time AND c.stage = :done
            WHERE {_IS_FORECAST}
              AND f.issued_time < cur.it
              AND f.issued_time >= cur.it - make_interval(hours => :prev_hours)
            GROUP BY f.disturbance_id
        )
        SELECT DISTINCT p.disturbance_id, p.iso3, {_NAME} AS name, NULL::int AS season FROM (
            SELECT e.disturbance_id, e.iso3 FROM storms.mfr_tracks_fcastonly_exposure e
            JOIN prev ON prev.disturbance_id = e.disturbance_id AND prev.it = e.issued_time
            WHERE e.admin_level = :al AND e.pop_exposed > 0
            UNION
            -- a country only the shifted tracks reached was alerted too, so
            -- it needs its final notice as well
            SELECT e.disturbance_id, e.iso3 FROM storms.mfr_tracks_scenario_exposure e
            JOIN prev ON prev.disturbance_id = e.disturbance_id AND prev.it = e.issued_time
            WHERE e.admin_level = :al AND e.pop_exposed > 0
        ) p
        JOIN storms.mfr_storms s ON s.disturbance_id = p.disturbance_id
    """)
    df = _df(engine, sql, _params(issued_time, al=_ADMIN_LEVEL, prev_hours=prev_hours))
    return [
        {"atcf_id": r.disturbance_id, "iso3": r.iso3, "name": r.name, "season": r.season}
        for r in df.itertuples()
    ]


@_cached
def fetch_all_prior_country_pairs(engine: Engine, atcf_ids: list[str], issued_time: datetime) -> dict:
    """{(system, iso3): last issued_time} for pairs with forecast exposure at
    any forecast before each system's current one."""
    if not atcf_ids:
        return {}
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT e.disturbance_id, e.iso3, MAX(e.issued_time) AS last_t FROM (
            SELECT disturbance_id, iso3, issued_time, pop_exposed, admin_level
            FROM storms.mfr_tracks_fcastonly_exposure
            UNION ALL
            SELECT disturbance_id, iso3, issued_time, pop_exposed, admin_level
            FROM storms.mfr_tracks_scenario_exposure
        ) e
        JOIN cur ON cur.disturbance_id = e.disturbance_id
        WHERE e.disturbance_id IN :ids AND e.admin_level = :al
          AND e.issued_time < cur.it AND e.pop_exposed > 0
        GROUP BY e.disturbance_id, e.iso3
    """).bindparams(bindparam("ids", expanding=True))
    df = _df(engine, sql, _params(issued_time, ids=list(atcf_ids), al=_ADMIN_LEVEL))
    return {(r.disturbance_id, r.iso3): r.last_t for r in df.itertuples()}


def _hist_name(storm_id, fallback) -> str:
    """'firinga_si_1989' -> 'FIRINGA'; unnamed systems -> 'Unnamed'."""
    if isinstance(storm_id, str) and "_si_" in storm_id:
        return storm_id.rsplit("_si_", 1)[0].replace("_", " ").upper()
    return fallback if isinstance(fallback, str) and fallback else "Unnamed"


@_cached
def fetch_historical_obsv_exposure(engine: Engine, iso3s: list[str], exclude_atcf_ids: list[str]) -> pd.DataFrame:
    """Whole-track exposure per past system (Météo-France's records).

    The current systems are left out by id: history built from this season's
    forecasts carries the same Météo-France disturbance_id. ``season`` is the
    season's END year (the table stores the start year).

    Returns columns: atcf_id, iso3, wind_speed_kt, pop_exposed, name, season.
    """
    cols = ["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"]
    if not iso3s:
        return pd.DataFrame(columns=cols)
    sql = text("""
        SELECT e.disturbance_id AS atcf_id, e.iso3, e.wind_speed_kt, e.pop_exposed,
               b.storm_id, s.name AS mfr_name, b.season + 1 AS season
        FROM storms.swio_hist_wind_exposure e
        JOIN storms.swio_hist_wind_buffers b
          ON b.disturbance_id = e.disturbance_id AND b.wind_speed_kt = e.wind_speed_kt
        LEFT JOIN storms.mfr_storms s ON s.disturbance_id = e.disturbance_id
        WHERE e.iso3 IN :iso3s AND e.admin_level = :al
    """).bindparams(bindparam("iso3s", expanding=True))
    df = _df(engine, sql, {"iso3s": list(iso3s), "al": _ADMIN_LEVEL})
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["name"] = [_hist_name(sid, nm) for sid, nm in zip(df["storm_id"], df["mfr_name"])]
    df = df[~df["atcf_id"].isin(set(exclude_atcf_ids))]
    return df.reindex(columns=cols).reset_index(drop=True)


@_cached
def fetch_admin_population(engine: Engine, iso3s: list[str]) -> dict[str, int]:
    """{iso3: total population} on the same sea-extended units as the exposure."""
    if not iso3s:
        return {}
    sql = text("""
        SELECT iso3, total_pop FROM storms.swio_admin_population
        WHERE admin_level = 0 AND iso3 IN :iso3s
    """).bindparams(bindparam("iso3s", expanding=True))
    df = _df(engine, sql, {"iso3s": list(iso3s)})
    return {r.iso3: int(r.total_pop) for r in df.itertuples()}


@_cached
def fetch_active_storm_meta(engine: Engine, issued_time: datetime) -> list[dict]:
    """Systems with a current forecast. Returns [{atcf_id, name, season}]."""
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT cur.disturbance_id, {_NAME} AS name, NULL::int AS season FROM cur
        JOIN storms.mfr_storms s ON s.disturbance_id = cur.disturbance_id
        ORDER BY cur.disturbance_id
    """)
    df = _df(engine, sql, _params(issued_time))
    return [{"atcf_id": r.disturbance_id, "name": r.name, "season": r.season} for r in df.itertuples()]


def fetch_all_monitored_countries(engine: Engine) -> list[str]:
    return list(SWIO_ISO3S)


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
            FROM storms.mfr_tracks_geo g
            JOIN storms.mfr_forecasts f
              ON f.disturbance_id = g.disturbance_id
             AND f.issued_time = g.issued_time AND f.kind = g.kind
            WHERE g.disturbance_id IN :ids AND {_IS_FORECAST}
              AND g.data_type = 'analysis' AND g.issued_time <= :t
              AND g.valid_time <= :t
            ORDER BY g.disturbance_id, g.valid_time, g.issued_time DESC
        ) o
        UNION ALL
        SELECT g.disturbance_id AS atcf_id, g.valid_time, g.wind_speed,
               'forecast' AS kind, g.geometry
        FROM storms.mfr_tracks_geo g
        JOIN cur ON cur.disturbance_id = g.disturbance_id AND cur.it = g.issued_time
        WHERE g.disturbance_id IN :ids AND g.kind = 'forecast'
          AND g.data_type = 'forecast' AND g.leadtime > 0 AND g.leadtime <= :maxlt
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
            FROM storms.mfr_tracks_obsv_buffers b
            WHERE b.disturbance_id IN :ids AND b.valid_time <= :t
            ORDER BY b.disturbance_id, b.wind_speed_kt, b.valid_time DESC
        ) o
        UNION ALL
        SELECT b.disturbance_id AS atcf_id, b.wind_speed_kt, 'forecast' AS kind, b.geometry
        FROM storms.mfr_tracks_fcastonly_buffers b
        JOIN cur ON cur.disturbance_id = b.disturbance_id AND cur.it = b.issued_time
        WHERE b.disturbance_id IN :ids
    """).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        gdf = gpd.read_postgis(sql, conn, geom_col="geometry",
                               params=_params(issued_time, ids=list(atcf_ids)))
    return gdf[cols]


@_cached
def fetch_scenario_exposure(engine: Engine, atcf_ids: list[str], issued_time: datetime) -> pd.DataFrame:
    """Per (system, country, wind): the highest and lowest forecast-only
    exposure across the uncertainty scenarios (see data_sp for the rule).

    Returns columns: atcf_id, iso3, wind_speed_kt, scen_max, scen_min,
    n_scenarios.
    """
    cols = ["atcf_id", "iso3", "wind_speed_kt", "scen_max", "scen_min",
            "n_scenarios"]
    if not atcf_ids:
        return pd.DataFrame(columns=cols)
    sql = text(f"""
        WITH {_CURRENT_CTE}
        SELECT e.disturbance_id AS atcf_id, e.iso3, e.wind_speed_kt,
               MAX(e.pop_exposed) AS scen_max,
               CASE WHEN COUNT(DISTINCT e.shift_deg) FILTER (WHERE e.pop_exposed > 0)
                         >= COALESCE(MAX(v.n_scenarios), :n)
                    THEN MIN(e.pop_exposed) ELSE 0 END AS scen_min,
               COALESCE(MAX(v.n_scenarios), :n) AS n_scenarios
        FROM storms.mfr_tracks_scenario_exposure e
        JOIN cur ON cur.disturbance_id = e.disturbance_id AND cur.it = e.issued_time
        LEFT JOIN storms.mfr_tracks_scenario_envelope v
          ON v.disturbance_id = e.disturbance_id AND v.issued_time = e.issued_time
         AND v.wind_speed_kt = e.wind_speed_kt
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
        FROM storms.mfr_tracks_scenario_envelope v
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


# --- Not available for this basin in v1: empty, NHC-shaped ------------------

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
