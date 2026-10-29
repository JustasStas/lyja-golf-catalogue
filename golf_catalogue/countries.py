"""Which country a course is in, when its region covers more than one.

Geofabrik cuts most extracts along national borders, so the region's ISO
code is the answer almost everywhere. The exceptions are the extracts that
hold two countries; for those a coarse outline of the smaller one decides.
A golf course is hundreds of metres across and none sits on these borders,
so a rough outline is enough.
"""

from __future__ import annotations

from shapely.geometry import Point, Polygon

# Northern Ireland (GB) inside Geofabrik's "ireland-and-northern-ireland".
# Simplified from the border, (lon, lat), about 2 km accuracy.
NORTHERN_IRELAND = Polygon([
    (-5.43, 54.48), (-5.53, 54.25), (-5.87, 54.10), (-6.10, 54.02), (-6.27, 54.10),
    (-6.63, 54.04), (-6.73, 54.18), (-7.03, 54.42), (-7.21, 54.30), (-7.56, 54.13),
    (-7.86, 54.21), (-8.17, 54.46), (-7.75, 54.62), (-7.55, 54.72), (-7.45, 54.90),
    (-7.25, 55.05), (-6.97, 55.23), (-6.40, 55.25), (-6.04, 55.25), (-5.70, 55.03),
    (-5.43, 54.48),
])

# Jersey (JE) inside Geofabrik's "guernsey-jersey" (the rest is Guernsey, GG).
JERSEY = Polygon([(-2.30, 49.15), (-1.95, 49.15), (-1.95, 49.30), (-2.30, 49.30), (-2.30, 49.15)])

# region country → [(polygon, country inside it)]
SPLITS: dict[str, list[tuple[Polygon, str]]] = {
    "IE": [(NORTHERN_IRELAND, "GB")],
    "GG": [(JERSEY, "JE")],
}


def resolve(region_country: str, tags: dict[str, str], lat: float, lon: float) -> str:
    """The course's ISO 3166-1 alpha-2 code."""
    for polygon, country in SPLITS.get(region_country, []):
        if polygon.covers(Point(lon, lat)):
            return country
    return region_country
