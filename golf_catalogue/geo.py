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


# MARK: Outline

# The published course outline (`outline` in course.schema.json): every
# outer ring of the `leisure=golf_course` area, Douglas–Peucker simplified in
# a local metric frame. The tolerance starts at OUTLINE_TOLERANCE_M and
# doubles until the outline fits in OUTLINE_MAX_POINTS, so a big or detailed
# course stays a few kilobytes. Inner rings (holes in the area) are dropped:
# the outline is a drawing of where the course is, not its exact area.
OUTLINE_TOLERANCE_M = 4.0
OUTLINE_MAX_POINTS = 96  # all rings together, closing points not counted
OUTLINE_MAX_RINGS = 8
OUTLINE_MIN_RING_SHARE = 0.05  # an outer ring under 5 % of the largest is a sliver
OUTLINE_DIGITS = 5  # ~1.1 m of latitude
_M_PER_DEG = 111_320.0


def outline(geom) -> list[list[list[float]]] | None:
    """The simplified outer rings of a shapely (Multi)Polygon in lon/lat, as
    `[[[lat, lon], ...], ...]`, largest ring first. Each ring is implicitly
    closed (the first point is not repeated) and has at least three points.
    None when nothing drawable is left."""
    if geom is None or geom.is_empty:
        return None
    from shapely.geometry import Polygon

    polygons = [g for g in getattr(geom, "geoms", [geom]) if g.geom_type == "Polygon" and not g.is_empty]
    if not polygons:
        return None
    rings = sorted(((ring_area_m2(p.exterior.coords), list(p.exterior.coords)) for p in polygons), key=lambda r: -r[0])
    largest = rings[0][0]
    if largest <= 0:
        return None
    rings = [coords for area, coords in rings if area >= largest * OUTLINE_MIN_RING_SHARE][:OUTLINE_MAX_RINGS]

    # A local equirectangular frame around the centre, in metres. Longitudes
    # are unwrapped against the centre so a course on the antimeridian stays
    # in one piece.
    minx, miny, maxx, maxy = geom.bounds
    lat0 = (miny + maxy) / 2
    lon0 = rings[0][0][0]
    kx = _M_PER_DEG * max(math.cos(math.radians(lat0)), 0.01)

    def to_xy(lon: float, lat: float) -> tuple[float, float]:
        return (((lon - lon0 + 180.0) % 360.0) - 180.0) * kx, (lat - lat0) * _M_PER_DEG

    def to_latlon(x: float, y: float) -> list[float]:
        lon = ((lon0 + x / kx + 180.0) % 360.0) - 180.0
        return [round(lat0 + y / _M_PER_DEG, OUTLINE_DIGITS), round(lon, OUTLINE_DIGITS)]

    projected = [Polygon([to_xy(lon, lat) for lon, lat in coords]) for coords in rings]
    tolerance = OUTLINE_TOLERANCE_M
    while True:
        out = []
        for poly in projected:
            simple = poly.simplify(tolerance, preserve_topology=True)
            if simple.is_empty or simple.geom_type != "Polygon":
                continue
            ring = []
            for x, y in list(simple.exterior.coords)[:-1]:
                point = to_latlon(x, y)
                if not ring or ring[-1] != point:
                    ring.append(point)
            if len(ring) > 1 and ring[0] == ring[-1]:
                ring.pop()
            if len(ring) >= 3:
                out.append(ring)
        if sum(len(r) for r in out) <= OUTLINE_MAX_POINTS or tolerance > 5_000:
            break
        tolerance *= 2
    # Rings still over the cap at a 5 km tolerance cannot happen for a real
    # course; keep the largest rings that fit rather than publish a big file.
    kept, total = [], 0
    for ring in out:
        if total + len(ring) > OUTLINE_MAX_POINTS:
            break
        kept.append(ring)
        total += len(ring)
    return kept or None
