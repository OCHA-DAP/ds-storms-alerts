"""Forecast landfall estimation from the NHC track.

Computed from our own data rather than pulled from PDC: the NHC forecast track
is already in the DB for every storm we alert on, whereas PDC needs an API key,
covers only the storms it carries, and derives its landfall from the same NHC
bulletin anyway. Reviewer note that prompted this: landfall time/place was the
one operationally-critical fact the alert didn't state.

A "landfall" here is the first point where the forecast track (including the
bridge segment from the last observed position) enters a country's polygon.
Times are interpolated linearly along the crossing segment; intensity is the
crossing segment's maximum of the two endpoint wind speeds — landfall sits
between forecast points, and understating a hurricane by interpolating across
its peak would be the wrong kind of error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import geopandas as gpd
from shapely.geometry import LineString, Point


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


def _track_points(tracks: gpd.GeoDataFrame) -> list[tuple[datetime, float, Point]]:
    """Forecast path as (valid_time, wind_kt, point), bridged from the last
    observed position so a landfall inside the first forecast interval isn't
    missed."""
    fcs = tracks[tracks["kind"] == "forecast"].sort_values("valid_time")
    if fcs.empty:
        return []
    pts: list[tuple[datetime, float, Point]] = []
    obs = tracks[tracks["kind"] == "observed"].sort_values("valid_time")
    if not obs.empty:
        last = obs.iloc[-1]
        pts.append((last["valid_time"], last.get("wind_speed"), last.geometry))
    for _, r in fcs.iterrows():
        pts.append((r["valid_time"], r.get("wind_speed"), r.geometry))
    return pts


def compute_landfalls(
    tracks: gpd.GeoDataFrame, country_geoms: dict[str, object]
) -> list[Landfall]:
    """First forecast landfall per country, ordered by time.

    country_geoms: iso3 -> (Multi)Polygon. Countries the track centre never
    enters produce no entry — brushing a coast with the wind field is exposure,
    not landfall.
    """
    pts = _track_points(tracks)
    if len(pts) < 2:
        return []

    out: list[Landfall] = []
    for iso3, geom in country_geoms.items():
        if geom is None or geom.is_empty:
            continue
        hit: Landfall | None = None
        for (t0, w0, p0), (t1, w1, p1) in zip(pts, pts[1:], strict=False):
            inside0 = geom.contains(p0)
            inside1 = geom.contains(p1)
            seg = LineString([p0, p1])
            if inside0:
                # Centre already over the country when the forecast starts:
                # report it, flagged, only if this is the very first segment —
                # otherwise it's just the continuation of a crossing we've
                # already recorded.
                if (t0, w0, p0) == pts[0]:
                    hit = Landfall(iso3, t0, w0, p0, already_inland=True)
                    break
                continue
            if not inside1 and not seg.intersects(geom):
                continue
            # Entering segment: the crossing point is the intersection with
            # the boundary closest to the segment start.
            inter = seg.intersection(geom.boundary)
            if inter.is_empty:
                # Fully-contained end point with no boundary crossing shouldn't
                # happen, but degrade to the end point rather than dying.
                cross, frac = p1, 1.0
            else:
                cands = (
                    list(inter.geoms) if hasattr(inter, "geoms") else [inter]
                )
                cands = [c for c in cands if isinstance(c, Point)] or [
                    Point(c.coords[0]) for c in cands if hasattr(c, "coords")
                ]
                if not cands:
                    continue
                cross = min(cands, key=lambda c: seg.project(c))
                frac = seg.project(cross) / seg.length if seg.length else 0.0
            t_cross = t0 + timedelta(
                seconds=frac * (t1 - t0).total_seconds()
            )
            # Max of the endpoints, not an interpolation: see module docstring.
            winds = [w for w in (w0, w1) if w is not None and not (
                isinstance(w, float) and math.isnan(w))]
            wind = max(winds) if winds else None
            hit = Landfall(iso3, t_cross, wind, cross, already_inland=False)
            break
        if hit is not None:
            out.append(hit)
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
