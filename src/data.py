import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import fsspec
import geopandas as gpd
import ocha_stratus as stratus
import pandas as pd
from sqlalchemy import Engine, bindparam, text

logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
    logging.WARNING
)

_FIELDMAPS_ADM1_URL = (
    "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm1_polygons.parquet"
)
_BOUNDARY_CACHE_DIR = Path(__file__).parents[1] / "data" / "adm1"
_NE_BACKGROUND_PATH = Path(__file__).parents[1] / "data" / "ne110m_countries.parquet"
_ADM1_COLS = ["iso_3", "adm0_name", "adm1_id", "adm1_name", "geometry"]
_BOUNDARY_SIMPLIFY_TOL = 0.001  # degrees (~100 m); sharp enough for adm1 display

# Shared blob location — same as ds-storms-pipeline. `global` is the
# team's conventional container for shared vector reference data.
# Path mirrors the upstream FieldMaps URL structure:
# https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm1_polygons.parquet
_BLOB_CONTAINER = "global"
_BLOB_BASE = "fieldmaps/edge-matched/humanitarian/intl/"
_BLOB_ADM1_PREFIX = _BLOB_BASE + "adm1/"
_BLOB_ADM0_PREFIX = _BLOB_BASE + "adm0/"

_ADMIN_LEVEL = 0
_WIND_SPEEDS_KT = (34, 50, 64)

# Auxiliary products (WSP polygons/exposure, and the GDACS/ADAM exposure, whose
# valid_time tracks the NHC advisory time) are paired with the track advisory by
# matching the advisory time OR exactly this many hours earlier — taking the later
# of the two when both exist. This covers both the WSP synoptic-grid lag
# (e.g. 15:00 advisory → 12:00 WSP) and a one-cycle lag in GDACS/ADAM publication.
_ISSUED_OFFSET_HOURS = 3


def fetch_fcast_exposure(engine: Engine, issued_time: datetime) -> pd.DataFrame:
    """Forecast-only exposure for all wind speeds at issued_time (admin0, > 0).

    Returns columns: atcf_id, iso3, wind_speed_kt, pop_exposed, name, season.
    """
    sql = text("""
        SELECT e.atcf_id, e.iso3, e.wind_speed_kt, e.pop_exposed,
               COALESCE(NULLIF(s.name, 'NaN'), ib.name) AS name,
               COALESCE(s.season, ib.season) AS season
        FROM storms.nhc_tracks_fcastonly_exposure e
        LEFT JOIN storms.nhc_storms s ON s.atcf_id = e.atcf_id
        LEFT JOIN storms.ibtracs_storms ib ON ib.atcf_id = e.atcf_id
        WHERE e.issued_time = :issued_time
          AND e.admin_level = :admin_level
          AND e.pop_exposed > 0
    """)
    with engine.connect() as conn:
        result = conn.execute(
            sql,
            {"issued_time": issued_time, "admin_level": _ADMIN_LEVEL},
        )
        return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


def fetch_current_obsv_exposure(
    engine: Engine, atcf_ids: list[str], issued_time: datetime
) -> pd.DataFrame:
    """Latest cumulative observed exposure per (atcf_id, iso3, wind_speed_kt).

    Uses valid_time <= issued_time.
    Returns columns: atcf_id, iso3, wind_speed_kt, pop_exposed, name, season.
    """
    cols = ["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"]
    if not atcf_ids:
        return pd.DataFrame(columns=cols)
    sql = text("""
        SELECT DISTINCT ON (e.atcf_id, e.iso3, e.wind_speed_kt)
          e.atcf_id, e.iso3, e.wind_speed_kt, e.pop_exposed,
          COALESCE(NULLIF(s.name, 'NaN'), ib.name) AS name,
          COALESCE(s.season, ib.season) AS season
        FROM storms.nhc_tracks_obsv_exposure e
        LEFT JOIN storms.nhc_storms s ON s.atcf_id = e.atcf_id
        LEFT JOIN storms.ibtracs_storms ib ON ib.atcf_id = e.atcf_id
        WHERE e.atcf_id IN :atcf_ids
          AND e.admin_level = :admin_level
          AND e.valid_time <= :issued_time
        ORDER BY e.atcf_id, e.iso3, e.wind_speed_kt, e.valid_time DESC
    """).bindparams(bindparam("atcf_ids", expanding=True))
    with engine.connect() as conn:
        result = conn.execute(
            sql,
            {
                "atcf_ids": atcf_ids,
                "issued_time": issued_time,
                "admin_level": _ADMIN_LEVEL,
            },
        )
        return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


def fetch_gdacs_current_exposure(
    engine: Engine, atcf_ids: list[str], issued_time: datetime
) -> pd.DataFrame:
    """GDACS exposure per (atcf_id, iso3, wind_speed_kt) for the given advisory.

    GDACS exposure has no issued_time, but its valid_time tracks the NHC advisory
    time. Match valid_time to the advisory time, or _ISSUED_OFFSET_HOURS earlier,
    keeping the later of the two when both exist — done at the SQL level by
    DISTINCT ON (atcf_id, iso3, wind_speed_kt) + ORDER BY valid_time DESC, so a
    single row per group is returned (no downstream dedup). Same rule as the WSP fetches.
    Returns columns: atcf_id, iso3, wind_speed_kt, pop_exposed, name, season.
    """
    cols = ["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"]
    if not atcf_ids:
        return pd.DataFrame(columns=cols)
    sql = text("""
        SELECT DISTINCT ON (lk.atcf_id, g.iso3, g.wind_speed_kt)
            lk.atcf_id, g.iso3, g.wind_speed_kt, g.pop_exposed,
            NULLIF(s.name, 'NaN') AS name, s.season
        FROM storms.gdacs_exposure g
        JOIN storms.storm_id_lookup lk ON lk.gdacs_eventid = g.gdacs_eventid
        LEFT JOIN storms.nhc_storms s ON s.atcf_id = lk.atcf_id
        WHERE lk.atcf_id IN :atcf_ids
          AND g.admin_level = :admin_level
          AND g.pop_exposed > 0
          AND g.valid_time IN (:t_exact, :t_prev)
        ORDER BY lk.atcf_id, g.iso3, g.wind_speed_kt, g.valid_time DESC
    """).bindparams(bindparam("atcf_ids", expanding=True))
    with engine.connect() as conn:
        result = conn.execute(
            sql,
            {
                "atcf_ids": atcf_ids,
                "admin_level": _ADMIN_LEVEL,
                "t_exact": issued_time,
                "t_prev": issued_time - timedelta(hours=_ISSUED_OFFSET_HOURS),
            },
        )
        return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


def fetch_adam_current_exposure(
    engine: Engine, atcf_ids: list[str], issued_time: datetime
) -> pd.DataFrame:
    """ADAM exposure per (atcf_id, iso3, wind_speed_kt) for the given advisory.

    ADAM exposure has no issued_time, but its valid_time tracks the NHC advisory
    time. Match valid_time to the advisory time, or _ISSUED_OFFSET_HOURS earlier,
    keeping the later of the two when both exist — done at the SQL level by
    DISTINCT ON (atcf_id, iso3, wind_speed_kt) + ORDER BY valid_time DESC, so a
    single row per group is returned (no downstream dedup). Same rule as the WSP fetches.
    Returns columns: atcf_id, iso3, wind_speed_kt, pop_exposed, name, season.
    """
    cols = ["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"]
    if not atcf_ids:
        return pd.DataFrame(columns=cols)
    sql = text("""
        SELECT DISTINCT ON (lk.atcf_id, a.iso3, a.wind_speed_kt)
            lk.atcf_id, a.iso3, a.wind_speed_kt, a.pop_exposed,
            NULLIF(s.name, 'NaN') AS name, s.season
        FROM storms.adam_exposure a
        JOIN storms.storm_id_lookup lk ON lk.adam_eventid = a.adam_eventid
        LEFT JOIN storms.nhc_storms s ON s.atcf_id = lk.atcf_id
        WHERE lk.atcf_id IN :atcf_ids
          AND a.admin_level = :admin_level
          AND a.pop_exposed > 0
          AND a.valid_time IN (:t_exact, :t_prev)
        ORDER BY lk.atcf_id, a.iso3, a.wind_speed_kt, a.valid_time DESC
    """).bindparams(bindparam("atcf_ids", expanding=True))
    with engine.connect() as conn:
        result = conn.execute(
            sql,
            {
                "atcf_ids": atcf_ids,
                "admin_level": _ADMIN_LEVEL,
                "t_exact": issued_time,
                "t_prev": issued_time - timedelta(hours=_ISSUED_OFFSET_HOURS),
            },
        )
        return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


def fetch_adam_historical_exposure(
    engine: Engine, iso3s: list[str], exclude_atcf_ids: list[str]
) -> pd.DataFrame:
    """Final ADAM exposure per (adam_eventid, iso3, wind_speed_kt) for past storms.

    Returns columns:
        adam_eventid, iso3, wind_speed_kt, pop_exposed, name, season.
    name/season fall back to f"ADAM {adam_eventid}" / valid_time year if the
    event is not linked to an NHC storm.
    """
    cols = ["adam_eventid", "iso3", "wind_speed_kt",
            "pop_exposed", "name", "season"]
    if not iso3s:
        return pd.DataFrame(columns=cols)
    sql = text("""
        SELECT DISTINCT ON (a.adam_eventid, a.iso3, a.wind_speed_kt)
            a.adam_eventid, a.iso3, a.wind_speed_kt, a.pop_exposed,
            s.name, s.season,
            EXTRACT(YEAR FROM a.valid_time)::int AS fallback_year,
            lk.atcf_id
        FROM storms.adam_exposure a
        LEFT JOIN storms.storm_id_lookup lk ON lk.adam_eventid = a.adam_eventid
        LEFT JOIN storms.nhc_storms s ON s.atcf_id = lk.atcf_id
        WHERE a.iso3 IN :iso3s
          AND a.admin_level = :admin_level
          AND a.pop_exposed > 0
        ORDER BY a.adam_eventid, a.iso3, a.wind_speed_kt, a.valid_time DESC
    """).bindparams(bindparam("iso3s", expanding=True))
    with engine.connect() as conn:
        result = conn.execute(
            sql,
            {"iso3s": iso3s, "admin_level": _ADMIN_LEVEL},
        )
        df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    df = df[~df["atcf_id"].isin(exclude_atcf_ids)].copy()
    df["name"] = df["name"].fillna(
        df["adam_eventid"].apply(lambda e: f"ADAM {e}")
    )
    df["season"] = df["season"].fillna(df["fallback_year"]).astype(int)
    return df[cols].reset_index(drop=True)


def fetch_gdacs_historical_exposure(
    engine: Engine, iso3s: list[str], exclude_atcf_ids: list[str]
) -> pd.DataFrame:
    """Final GDACS exposure per (gdacs_eventid, iso3, wind_speed_kt) for past storms.

    Returns columns:
        gdacs_eventid, iso3, wind_speed_kt, pop_exposed, name, season.
    name/season come from nhc_storms via storm_id_lookup; when not available,
    name falls back to f"GDACS {gdacs_eventid}" and season to the year of
    the GDACS valid_time.
    """
    cols = ["gdacs_eventid", "iso3", "wind_speed_kt",
            "pop_exposed", "name", "season"]
    if not iso3s:
        return pd.DataFrame(columns=cols)
    sql = text("""
        SELECT DISTINCT ON (g.gdacs_eventid, g.iso3, g.wind_speed_kt)
            g.gdacs_eventid, g.iso3, g.wind_speed_kt, g.pop_exposed,
            s.name, s.season,
            EXTRACT(YEAR FROM g.valid_time)::int AS fallback_year,
            lk.atcf_id
        FROM storms.gdacs_exposure g
        LEFT JOIN storms.storm_id_lookup lk ON lk.gdacs_eventid = g.gdacs_eventid
        LEFT JOIN storms.nhc_storms s ON s.atcf_id = lk.atcf_id
        WHERE g.iso3 IN :iso3s
          AND g.admin_level = :admin_level
          AND g.pop_exposed > 0
        ORDER BY g.gdacs_eventid, g.iso3, g.wind_speed_kt, g.valid_time DESC
    """).bindparams(bindparam("iso3s", expanding=True))
    with engine.connect() as conn:
        result = conn.execute(
            sql,
            {"iso3s": iso3s, "admin_level": _ADMIN_LEVEL},
        )
        df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    df = df[~df["atcf_id"].isin(exclude_atcf_ids)].copy()
    df["name"] = df["name"].fillna(
        df["gdacs_eventid"].apply(lambda e: f"GDACS {e}")
    )
    df["season"] = df["season"].fillna(df["fallback_year"]).astype(int)
    return df[cols].reset_index(drop=True)


def fetch_track_geo(
    engine: Engine, atcf_ids: list[str], issued_time: datetime
) -> gpd.GeoDataFrame:
    """Storm track points for plotting forecast + observed paths.

    For each atcf_id, returns:
      - observed points: leadtime=0 rows with valid_time <= issued_time
      - forecast points: rows where issued_time = :issued_time and leadtime > 0

    Returns GeoDataFrame with columns: atcf_id, valid_time, kind, geometry.
    kind is one of 'observed' or 'forecast'.
    """
    if not atcf_ids:
        return gpd.GeoDataFrame(
            columns=["atcf_id", "valid_time", "kind", "geometry"], crs="EPSG:4326"
        )
    sql = text("""
        SELECT atcf_id, valid_time, geometry, 'observed' AS kind
        FROM storms.nhc_tracks_geo
        WHERE atcf_id IN :atcf_ids
          AND leadtime = 0
          AND valid_time <= :issued_time
        UNION ALL
        SELECT atcf_id, valid_time, geometry, 'forecast' AS kind
        FROM storms.nhc_tracks_geo
        WHERE atcf_id IN :atcf_ids
          AND issued_time = :issued_time
          AND leadtime > 0
    """).bindparams(bindparam("atcf_ids", expanding=True))
    return gpd.read_postgis(
        sql,
        engine,
        params={"atcf_ids": atcf_ids, "issued_time": issued_time},
        geom_col="geometry",
    )


def _load_one_adm1(iso3: str) -> gpd.GeoDataFrame:
    """Load adm1 for a single country.

    Priority:
    1. Shared blob (raster/fieldmaps/adm1/{iso3}.parquet) — same source as
       ds-storms-pipeline; full-res FieldMaps, simplified here for display.
    2. Local repo file (data/adm1/{iso3}.parquet) — pre-simplified fallback.
    3. FieldMaps URL — last resort; writes result to local repo for next time.
    """
    local_path = _BOUNDARY_CACHE_DIR / f"{iso3}.parquet"

    # 1. Try blob — already simplified at 0.001° by mirror_fieldmaps_to_blob.py
    try:
        raw_bytes = stratus.load_blob_data(
            f"{_BLOB_ADM1_PREFIX}{iso3}.parquet",
            container_name=_BLOB_CONTAINER,
        )
        gdf = gpd.read_parquet(BytesIO(raw_bytes))[list(_ADM1_COLS)]
        return gdf.reset_index(drop=True)
    except Exception:
        pass

    # 2. Local repo file
    if local_path.exists():
        return gpd.read_parquet(local_path)

    # 3. FieldMaps URL — download and cache locally
    with fsspec.open(_FIELDMAPS_ADM1_URL, "rb") as f:
        raw = gpd.read_parquet(
            f, columns=_ADM1_COLS, filters=[("iso_3", "==", iso3)]
        )
    raw = raw.copy()
    raw["geometry"] = raw.geometry.simplify(
        _BOUNDARY_SIMPLIFY_TOL, preserve_topology=True
    )
    raw = raw.reset_index(drop=True)
    _BOUNDARY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raw.to_parquet(local_path)
    return raw


def _load_adm1_from_cache(iso3s: list[str]) -> gpd.GeoDataFrame:
    """Load adm1 for multiple countries in parallel."""
    with ThreadPoolExecutor(max_workers=min(16, len(iso3s))) as ex:
        parts = list(ex.map(_load_one_adm1, iso3s))
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)


def _load_one_adm0(iso3: str) -> gpd.GeoDataFrame:
    """Load pre-dissolved adm0 for a single country.

    The adm0 blob (fieldmaps/adm0/{iso3}.parquet) contains only iso_3 + geometry,
    already simplified at 0.001° by the mirror script. Falls back to dissolving
    from adm1 if the adm0 blob isn't available.
    """
    try:
        raw_bytes = stratus.load_blob_data(
            f"{_BLOB_ADM0_PREFIX}{iso3}.parquet",
            container_name=_BLOB_CONTAINER,
        )
        return gpd.read_parquet(BytesIO(raw_bytes)).reset_index(drop=True)
    except Exception:
        adm1 = _load_one_adm1(iso3)
        dissolved = adm1.dissolve(by="iso_3", as_index=False, aggfunc="first")
        return dissolved[["iso_3", "geometry"]].reset_index(drop=True)


def load_adm0_boundaries(iso3s: list[str]) -> gpd.GeoDataFrame:
    """Load adm0 boundaries from the dedicated adm0 blob (pre-dissolved).

    Returns columns: iso_3, geometry (adm0_name not available in adm0 blob).
    """
    if not iso3s:
        return gpd.GeoDataFrame(columns=["iso_3", "geometry"], crs="EPSG:4326")
    with ThreadPoolExecutor(max_workers=min(16, len(iso3s))) as ex:
        parts = list(ex.map(_load_one_adm0, iso3s))
    return gpd.GeoDataFrame(
        pd.concat(parts, ignore_index=True), crs=parts[0].crs
    ).reset_index(drop=True)


def load_background_countries() -> gpd.GeoDataFrame:
    """Load Natural Earth 110m world country outlines for map backgrounds."""
    return gpd.read_parquet(_NE_BACKGROUND_PATH)


def load_adm1_boundaries(iso3s: list[str]) -> gpd.GeoDataFrame:
    """Load adm1 boundaries for the given iso3s.

    Returns columns: iso_3, adm0_name, adm1_id, adm1_name, geometry.
    """
    if not iso3s:
        return gpd.GeoDataFrame(columns=_ADM1_COLS, crs="EPSG:4326")
    return _load_adm1_from_cache(iso3s)


def fetch_wsp_fcastonly_exposure(
    engine: Engine, atcf_ids: list[str], issued_time: datetime
) -> pd.DataFrame:
    """WSP fcastonly exposure per (atcf_id, iso3, wind_threshold_kt, percentage).

    Returns columns:
        atcf_id, iso3, wind_threshold_kt, percentage, pop_exposed.
    """
    cols = ["atcf_id", "iso3", "wind_threshold_kt", "percentage", "pop_exposed"]
    if not atcf_ids:
        return pd.DataFrame(columns=cols)
    # Match the WSP issued at the advisory time, or exactly _ISSUED_OFFSET_HOURS
    # earlier; per storm keep the later of the two if both exist (see note above).
    sql = text("""
        WITH cand AS (
            SELECT atcf_id, pcode AS iso3, wind_threshold_kt, percentage,
                   pop_exposed, issued_time
            FROM storms.nhc_wsp_fcastonly_exposure
            WHERE atcf_id IN :atcf_ids
              AND issued_time IN (:t_exact, :t_prev)
              AND admin_level = :admin_level
        ),
        latest AS (
            SELECT atcf_id, MAX(issued_time) AS it FROM cand GROUP BY atcf_id
        )
        SELECT c.atcf_id, c.iso3, c.wind_threshold_kt, c.percentage, c.pop_exposed
        FROM cand c
        JOIN latest l ON c.atcf_id = l.atcf_id AND c.issued_time = l.it
        ORDER BY c.atcf_id, c.iso3, c.wind_threshold_kt, c.percentage
    """).bindparams(bindparam("atcf_ids", expanding=True))
    with engine.connect() as conn:
        result = conn.execute(
            sql,
            {
                "atcf_ids": atcf_ids,
                "t_exact": issued_time,
                "t_prev": issued_time - timedelta(hours=_ISSUED_OFFSET_HOURS),
                "admin_level": _ADMIN_LEVEL,
            },
        )
        return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


def fetch_wsp_fcastonly_polygons(
    engine: Engine,
    atcf_ids: list[str],
    issued_time: datetime,
    wind_threshold_kt: int = 50,
) -> gpd.GeoDataFrame:
    """Fcastonly WSP polygons for the active storms at one wind threshold.

    Returns GeoDataFrame with columns: atcf_id, percentage, geometry.
    """
    if not atcf_ids:
        return gpd.GeoDataFrame(
            columns=["atcf_id", "percentage", "geometry"], crs="EPSG:4326"
        )
    # Match the WSP issued at the advisory time, or exactly _ISSUED_OFFSET_HOURS
    # earlier; per storm keep the later of the two if both exist (see note above).
    sql = text("""
        WITH cand AS (
            SELECT atcf_id, percentage, geometry, issued_time
            FROM storms.nhc_wsp_fcastonly_polygon
            WHERE atcf_id IN :atcf_ids
              AND issued_time IN (:t_exact, :t_prev)
              AND wind_threshold_kt = :wind_threshold_kt
              AND geometry IS NOT NULL
        ),
        latest AS (
            SELECT atcf_id, MAX(issued_time) AS it FROM cand GROUP BY atcf_id
        )
        SELECT c.atcf_id, c.percentage, c.geometry
        FROM cand c
        JOIN latest l ON c.atcf_id = l.atcf_id AND c.issued_time = l.it
    """).bindparams(bindparam("atcf_ids", expanding=True))
    return gpd.read_postgis(
        sql, engine,
        params={
            "atcf_ids": atcf_ids,
            "t_exact": issued_time,
            "t_prev": issued_time - timedelta(hours=_ISSUED_OFFSET_HOURS),
            "wind_threshold_kt": wind_threshold_kt,
        },
        geom_col="geometry",
    )


def fetch_prev_any_pairs(
    engine: Engine, issued_time: datetime, prev_hours: int = 6
) -> list[dict]:
    """Return (atcf_id, iso3, name, season) rows for storm-country pairs that had
    non-zero forecasted exposure at ANY wind speed in the advisory issued in the
    previous 6-hour window [issued_time - prev_hours, issued_time).

    Using a single 6-hour window (matching the NHC advisory cadence) means the
    final-update notice fires exactly once — in the run immediately after a
    storm's last advisory — rather than repeatedly for the following 7 days.
    """
    cutoff = issued_time - timedelta(hours=prev_hours)
    sql = text("""
        WITH prev_track_times AS (
            SELECT atcf_id, MAX(issued_time) AS prev_time
            FROM storms.nhc_tracks_fcastonly_exposure
            WHERE issued_time < :issued_time
              AND issued_time >= :cutoff
              AND admin_level = :admin_level
              AND pop_exposed > 0
            GROUP BY atcf_id
        ),
        track_pairs AS (
            SELECT e.atcf_id, e.iso3,
                   COALESCE(NULLIF(s.name, 'NaN'), ib.name) AS name,
                   COALESCE(s.season, ib.season) AS season
            FROM storms.nhc_tracks_fcastonly_exposure e
            JOIN prev_track_times p
              ON e.atcf_id = p.atcf_id AND e.issued_time = p.prev_time
            LEFT JOIN storms.nhc_storms s ON s.atcf_id = e.atcf_id
            LEFT JOIN storms.ibtracs_storms ib ON ib.atcf_id = e.atcf_id
            WHERE e.admin_level = :admin_level AND e.pop_exposed > 0
        ),
        prev_wsp_times AS (
            SELECT atcf_id, MAX(issued_time) AS prev_time
            FROM storms.nhc_wsp_fcastonly_exposure
            WHERE issued_time < :issued_time
              AND issued_time >= :cutoff
              AND admin_level = :admin_level
              AND pop_exposed > 0
            GROUP BY atcf_id
        ),
        wsp_pairs AS (
            SELECT e.atcf_id, e.pcode AS iso3,
                   COALESCE(NULLIF(s.name, 'NaN'), ib.name) AS name,
                   COALESCE(s.season, ib.season) AS season
            FROM storms.nhc_wsp_fcastonly_exposure e
            JOIN prev_wsp_times p
              ON e.atcf_id = p.atcf_id AND e.issued_time = p.prev_time
            LEFT JOIN storms.nhc_storms s ON s.atcf_id = e.atcf_id
            LEFT JOIN storms.ibtracs_storms ib ON ib.atcf_id = e.atcf_id
            WHERE e.admin_level = :admin_level AND e.pop_exposed > 0
        )
        SELECT atcf_id, iso3, name, season FROM track_pairs
        UNION
        SELECT atcf_id, iso3, name, season FROM wsp_pairs
    """)
    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {
                "issued_time": issued_time,
                "cutoff": cutoff,
                "admin_level": _ADMIN_LEVEL,
            },
        ).fetchall()
    return [{"atcf_id": r[0], "iso3": r[1], "name": r[2], "season": r[3]} for r in rows]


def fetch_buffers(
    engine: Engine, atcf_ids: list[str], issued_time: datetime
) -> gpd.GeoDataFrame:
    """Observed and forecast wind buffer polygons at all wind speeds (34/50/64 kt).

    Observed: per (atcf_id, wind_speed_kt), the latest valid_time <= issued_time
    row from nhc_tracks_obsv_buffers.
    Forecast: rows for the given issued_time from nhc_tracks_fcastonly_buffers.

    Returns GeoDataFrame with columns: atcf_id, wind_speed_kt, kind, geometry.
    """
    if not atcf_ids:
        return gpd.GeoDataFrame(
            columns=["atcf_id", "wind_speed_kt", "kind", "geometry"], crs="EPSG:4326"
        )
    sql = text("""
        SELECT atcf_id, wind_speed_kt, kind, geometry FROM (
            SELECT DISTINCT ON (atcf_id, wind_speed_kt)
                atcf_id, wind_speed_kt, geometry, 'observed' AS kind
            FROM storms.nhc_tracks_obsv_buffers
            WHERE atcf_id IN :atcf_ids
              AND valid_time <= :issued_time
            ORDER BY atcf_id, wind_speed_kt, valid_time DESC
        ) o
        UNION ALL
        SELECT atcf_id, wind_speed_kt, kind, geometry FROM (
            SELECT atcf_id, wind_speed_kt, geometry, 'forecast' AS kind
            FROM storms.nhc_tracks_fcastonly_buffers
            WHERE atcf_id IN :atcf_ids
              AND issued_time = :issued_time
        ) f
    """).bindparams(bindparam("atcf_ids", expanding=True))
    return gpd.read_postgis(
        sql,
        engine,
        params={"atcf_ids": atcf_ids, "issued_time": issued_time},
        geom_col="geometry",
    )


def fetch_historical_obsv_exposure(
    engine: Engine, iso3s: list[str], exclude_atcf_ids: list[str]
) -> pd.DataFrame:
    """Final cumulative observed exposure per (atcf_id, iso3, wind_speed_kt).

    Latest valid_time row per storm/country/wind-speed, with active atcf_ids
    excluded.
    Returns columns: atcf_id, iso3, wind_speed_kt, pop_exposed, name, season.
    """
    cols = ["atcf_id", "iso3", "wind_speed_kt", "pop_exposed", "name", "season"]
    if not iso3s:
        return pd.DataFrame(columns=cols)
    sql = text("""
        SELECT DISTINCT ON (e.atcf_id, e.iso3, e.wind_speed_kt)
          e.atcf_id, e.iso3, e.wind_speed_kt, e.pop_exposed,
          COALESCE(NULLIF(s.name, 'NaN'), ib.name) AS name,
          COALESCE(s.season, ib.season) AS season
        FROM storms.nhc_tracks_obsv_exposure e
        LEFT JOIN storms.nhc_storms s ON s.atcf_id = e.atcf_id
        LEFT JOIN storms.ibtracs_storms ib ON ib.atcf_id = e.atcf_id
        WHERE e.iso3 IN :iso3s
          AND e.admin_level = :admin_level
        ORDER BY e.atcf_id, e.iso3, e.wind_speed_kt, e.valid_time DESC
    """).bindparams(bindparam("iso3s", expanding=True))
    with engine.connect() as conn:
        result = conn.execute(
            sql,
            {"iso3s": iso3s, "admin_level": _ADMIN_LEVEL},
        )
        df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    return df[~df["atcf_id"].isin(exclude_atcf_ids)].reset_index(drop=True)


def fetch_all_prior_country_pairs(
    engine: Engine, atcf_ids: list[str], issued_time: datetime
) -> dict[tuple[str, str], datetime]:
    """Return {(atcf_id, iso3): last_issued_time} for all storm-country pairs that
    had fcast/WSP exposure at any advisory before issued_time."""
    if not atcf_ids:
        return {}
    sql = text("""
        SELECT atcf_id, iso3, MAX(last_time) AS last_issued_time FROM (
            SELECT atcf_id, iso3, MAX(issued_time) AS last_time
            FROM storms.nhc_tracks_fcastonly_exposure
            WHERE atcf_id IN :atcf_ids AND admin_level = :admin_level
              AND issued_time < :issued_time AND pop_exposed > 0
            GROUP BY atcf_id, iso3
            UNION ALL
            SELECT atcf_id, pcode AS iso3, MAX(issued_time) AS last_time
            FROM storms.nhc_wsp_fcastonly_exposure
            WHERE atcf_id IN :atcf_ids AND admin_level = :admin_level
              AND issued_time < :issued_time AND pop_exposed > 0
            GROUP BY atcf_id, pcode
        ) sub
        GROUP BY atcf_id, iso3
    """).bindparams(bindparam("atcf_ids", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(sql, {
            "atcf_ids": atcf_ids,
            "admin_level": _ADMIN_LEVEL,
            "issued_time": issued_time,
        }).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def fetch_admin_population(engine: Engine, iso3s: list[str]) -> dict[str, int]:
    """Return {iso3: total_pop} from storms.admin_population at admin_level=0."""
    if not iso3s:
        return {}
    sql = text("""
        SELECT iso3, total_pop FROM storms.admin_population
        WHERE admin_level = 0 AND iso3 IN :iso3s
    """).bindparams(bindparam("iso3s", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(sql, {"iso3s": iso3s}).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def fetch_active_storm_meta(engine: Engine, issued_time: datetime) -> list[dict]:
    """Return basic metadata for all storms with forecast track data at issued_time.

    Sourced from nhc_tracks_geo (raw forecast points), so it detects active
    storms regardless of whether they affect any monitored country.
    Returns list of {atcf_id, name, season} dicts.
    """
    sql = text("""
        SELECT DISTINCT t.atcf_id,
            COALESCE(NULLIF(s.name, 'NaN'), ib.name) AS name,
            COALESCE(s.season, ib.season) AS season
        FROM storms.nhc_tracks_geo t
        LEFT JOIN storms.nhc_storms s ON s.atcf_id = t.atcf_id
        LEFT JOIN storms.ibtracs_storms ib ON ib.atcf_id = t.atcf_id
        WHERE t.issued_time = :issued_time
          AND t.leadtime > 0
        ORDER BY t.atcf_id
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"issued_time": issued_time}).fetchall()
    return [{"atcf_id": r[0], "name": r[1], "season": r[2]} for r in rows]


def fetch_all_monitored_countries(engine: Engine) -> list[str]:
    """Return all iso3s that have ever had non-zero exposure in either fcast table."""
    sql = text("""
        SELECT DISTINCT iso3 FROM storms.nhc_tracks_fcastonly_exposure
        WHERE admin_level = :admin_level AND pop_exposed > 0
        UNION
        SELECT DISTINCT pcode AS iso3 FROM storms.nhc_wsp_fcastonly_exposure
        WHERE admin_level = :admin_level AND pop_exposed > 0
        ORDER BY iso3
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"admin_level": _ADMIN_LEVEL}).fetchall()
    return [r[0] for r in rows]
