"""Small geodesy helpers: bearings, distances and areas on the WGS84 sphere.

The spherical earth (R = 6 371 008.8 m) is plenty for golf holes: the error
against the ellipsoid is well under 0.5 %, far below what a forecast wind
direction can resolve.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The initial great-circle bearing from point 1 to point 2, degrees
    clockwise from true north, in [0, 360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    bearing = math.degrees(math.atan2(y, x)) % 360.0
    # -0.0 % 360 is 0.0 but 359.99999 rounds up; keep it in range.
    return 0.0 if bearing >= 360.0 else bearing


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def path_length_m(points: Sequence[tuple[float, float]]) -> float:
    """Summed length of a (lat, lon) polyline, metres."""
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))


def ring_area_m2(ring: Iterable[tuple[float, float]]) -> float:
    """Area of a (lon, lat) ring on the sphere, m², by the spherical-excess
    formula used by most GIS tools for small polygons."""
    pts = list(ring)
    if len(pts) < 3:
        return 0.0
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(pts, pts[1:] + pts[:1]):
        total += math.radians(lon2 - lon1) * (2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    return abs(total * EARTH_RADIUS_M ** 2 / 2.0)


def geometry_area_ha(geom) -> float:
    """Area of a shapely (Multi)Polygon in lon/lat, hectares."""
    if geom is None or geom.is_empty:
        return 0.0
    polygons = getattr(geom, "geoms", [geom])
    area = 0.0
    for poly in polygons:
        if poly.geom_type != "Polygon":
            continue
        area += ring_area_m2(poly.exterior.coords)
        for interior in poly.interiors:
            area -= ring_area_m2(interior.coords)
    return max(area, 0.0) / 10_000.0


def metres_to_degrees(metres: float, lat: float) -> float:
    """A rough degrees-of-arc equivalent of a distance at a latitude, for
    buffering in lon/lat space (the larger of the two axes, so a buffer
    never falls short)."""
    lat_deg = metres / 111_320.0
    lon_deg = metres / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    return max(lat_deg, lon_deg)
