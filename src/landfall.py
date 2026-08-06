"""Forecast landfall estimation from the NHC track.

Computed from our own data rather than pulled from PDC: the NHC forecast track
is already in the DB for every storm we alert on, whereas PDC needs an API key,
covers only the storms it carries, and derives its landfall from the same NHC
bulletin anyway. Reviewer note that prompted this: landfall time/place was the
one operationally-critical fact the alert didn't state.

A "landfall" here is the first point where the forecast track (including the
bridge from the last observed position) enters a country's polygon. The track
is densified the same way the wind buffers are built — PCHIP interpolation of
lat/lon on a 30-minute grid, linear for wind speed, mirroring
``ocha_lens.utils.storm.interpolate_track`` (the function
``calculate_wind_buffers_gdf`` uses in ds-storms-pipeline) — so the landfall
point sits on the same curved path the swaths on the map are built from.
The full ocha-lens dependency chain (xarray, netcdf4, ...) is deliberately
not imported for these thirty lines; if lens's interpolation ever changes,
change this to match.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import geopandas as gpd
import numpy as np
from scipy.interpolate import PchipInterpolator
from shapely.geometry import Point


@dataclass(frozen=True, slots=True)
class Landfall:
    iso3: str
    time: datetime          # naive UTC, like everything else in the pipeline
    wind_speed_kt: float | None
    point: Point
    already_inland: bool    # centre already over this country at forecast start


def saffir_simpson(wind_kt: float | None) -> str:
    """Category label for a max sustained wind in kt."""
    if wind_kt is None or math.isnan(wind_kt):
        return ""
    if wind_kt >= 137:
        return "Category 5"
    if wind_kt >= 113:
        return "Category 4"
    if wind_kt >= 96:
        return "Category 3"
    if wind_kt >= 83:
        return "Category 2"
    if wind_kt >= 64:
        return "Category 1"
    if wind_kt >= 34:
        return "tropical storm"
    return "tropical depression"


def densify_track(
    tracks: gpd.GeoDataFrame, freq_minutes: int = 30
) -> tuple[list[datetime], np.ndarray, np.ndarray, np.ndarray | None]:
    """The forecast path (bridged from the last observed position) on a dense
    time grid: (times, lons, lats, winds). winds is None when unavailable.

    Mirrors ocha-lens interpolate_track: PCHIP for lat/lon when 3+ points,
    linear otherwise; linear for wind. No antimeridian handling — this system
    alerts on NHC (Atlantic / East Pacific) storms, which don't cross it.
    """
    fcs = tracks[tracks["kind"] == "forecast"].sort_values("valid_time")
    if fcs.empty:
        return [], np.array([]), np.array([]), None
    obs = tracks[tracks["kind"] == "observed"].sort_values("valid_time")
    rows = ([obs.iloc[-1]] if not obs.empty else []) + [
        r for _, r in fcs.iterrows()
    ]
    seen: set = set()
    times, lons, lats, winds = [], [], [], []
    for r in rows:
        tv = r["valid_time"]
        if tv in seen or r.geometry is None or r.geometry.is_empty:
            continue
        seen.add(tv)
        times.append(tv)
        lons.append(r.geometry.x)
        lats.append(r.geometry.y)
        w = r.get("wind_speed")
        winds.append(
            float(w) if w is not None and not (
                isinstance(w, float) and math.isnan(w)) else math.nan
        )
    if len(times) < 2:
        return [], np.array([]), np.array([]), None

    t0 = times[0]
    x = np.array([(tv - t0).total_seconds() for tv in times])
    step = freq_minutes * 60
    x_new = np.arange(x[0], x[-1] + 1, step)
    if x_new[-1] < x[-1]:
        x_new = np.append(x_new, x[-1])

    if len(times) == 2:
        lon_new = np.interp(x_new, x, lons)
        lat_new = np.interp(x_new, x, lats)
    else:
        lon_new = PchipInterpolator(x, lons)(x_new)
        lat_new = PchipInterpolator(x, lats)(x_new)

    w_arr = np.array(winds)
    ok = ~np.isnan(w_arr)
    winds_new = (
        np.interp(x_new, x[ok], w_arr[ok]) if ok.sum() >= 2 else None
    )
    times_new = [t0 + timedelta(seconds=float(s)) for s in x_new]
    return times_new, lon_new, lat_new, winds_new


def compute_landfalls(
    tracks: gpd.GeoDataFrame, country_geoms: dict[str, object]
) -> list[Landfall]:
    """First forecast landfall per country, ordered by time.

    country_geoms: iso3 -> (Multi)Polygon. Countries the track centre never
    enters produce no entry — brushing a coast with the wind field is exposure,
    not landfall. Resolution is the 30-minute densified track, so times are
    good to about +/- 15 minutes.
    """
    times, lons, lats, winds = densify_track(tracks)
    if not times:
        return []
    pts = [Point(x, y) for x, y in zip(lons, lats, strict=True)]

    out: list[Landfall] = []
    for iso3, geom in country_geoms.items():
        if geom is None or geom.is_empty:
            continue
        for i, pt in enumerate(pts):
            if not geom.contains(pt):
                continue
            wind = (
                float(winds[i]) if winds is not None
                and not math.isnan(winds[i]) else None
            )
            out.append(Landfall(
                iso3, times[i], wind, pt, already_inland=(i == 0)
            ))
            break
    out.sort(key=lambda lf: lf.time)
    return out


def nearest_adm1_name(point: Point, adm1_gdf: gpd.GeoDataFrame, iso3: str) -> str:
    """Name of the admin-1 unit nearest the landfall point, "" if unknown.

    Nearest rather than containing: the crossing point sits exactly on the
    coastline, where containment is a coin flip.
    """
    sub = adm1_gdf[adm1_gdf["iso_3"] == iso3]
    sub = sub[sub["adm1_name"].notna()]
    if sub.empty:
        return ""
    dists = sub.geometry.distance(point)
    name = sub.loc[dists.idxmin(), "adm1_name"]
    return str(name) if name else ""
