"""Writes the small `.osm` fixtures the tests read. Run it after changing one:

    python tests/fixtures/make_fixtures.py

The courses are made up; their numbers are chosen so each test has one thing
to look at.
"""

from __future__ import annotations

import math
from pathlib import Path
from xml.sax.saxutils import quoteattr

HERE = Path(__file__).resolve().parent


class Osm:
    def __init__(self, base: int) -> None:
        """`base` keeps each file's generated ids apart, so fixtures can be
        built together as one world."""
        self.nodes: list[tuple[int, str]] = []
        self.ways: list[tuple[int, str]] = []
        self.relations: list[tuple[int, str]] = []
        self.next_node = base * 10_000 + 1000
        self.next_way = base * 10_000 + 100

    def node(self, lat: float, lon: float, tags: dict | None = None, id: int | None = None) -> int:
        nid = id or self.next_node
        if id is None:
            self.next_node += 1
        body = "".join(f"<tag k={quoteattr(k)} v={quoteattr(str(v))}/>" for k, v in (tags or {}).items())
        self.nodes.append((nid, f'<node id="{nid}" version="1" lat="{lat:.7f}" lon="{lon:.7f}">{body}</node>'))
        return nid

    def way(self, refs: list[int], tags: dict, id: int | None = None) -> int:
        wid = id or self.next_way
        if id is None:
            self.next_way += 1
        nds = "".join(f'<nd ref="{r}"/>' for r in refs)
        body = "".join(f"<tag k={quoteattr(k)} v={quoteattr(str(v))}/>" for k, v in tags.items())
        self.ways.append((wid, f'<way id="{wid}" version="1">{nds}{body}</way>'))
        return wid

    def relation(self, members: list[int], tags: dict, id: int) -> int:
        body = "".join(f'<member type="way" ref="{m}" role=""/>' for m in members)
        body += "".join(f"<tag k={quoteattr(k)} v={quoteattr(str(v))}/>" for k, v in tags.items())
        self.relations.append((id, f'<relation id="{id}" version="1">{body}</relation>'))
        return id

    def area(self, south: float, west: float, north: float, east: float, tags: dict, id: int | None = None) -> int:
        corners = [(south, west), (south, east), (north, east), (north, west)]
        refs = [self.node(lat, lon) for lat, lon in corners]
        return self.way(refs + [refs[0]], tags, id)

    def hole(self, lat: float, lon: float, bearing: float, length: float, tags: dict, turn: float | None = None) -> tuple[int, float, float]:
        """A hole from a tee at (lat, lon): straight, or a dogleg turning by
        `turn` degrees two thirds of the way. Returns (way id, green lat, lon)."""
        def step(lat, lon, b, d):
            dlat = d * math.cos(math.radians(b)) / 111_320
            dlon = d * math.sin(math.radians(b)) / (111_320 * math.cos(math.radians(lat)))
            return lat + dlat, lon + dlon
        points = [(lat, lon)]
        if turn is None:
            points.append(step(lat, lon, bearing, length))
        else:
            mid = step(lat, lon, bearing, length * 2 / 3)
            points += [mid, step(*mid, bearing + turn, length / 3)]
        refs = [self.node(a, b) for a, b in points]
        return self.way(refs, {"golf": "hole", **tags}), points[-1][0], points[-1][1]

    def write(self, name: str) -> None:
        xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="lyja-fixtures">']
        # Sorted by id within each type, as OSM files are.
        for items in (self.nodes, self.ways, self.relations):
            xml += [text for _, text in sorted(items)]
        xml.append("</osm>")
        (HERE / name).write_text("\n".join(xml) + "\n", encoding="utf-8")


PARS_18 = [4, 4, 3, 5, 4, 4, 3, 4, 5, 4, 3, 4, 5, 4, 4, 3, 5, 4]
LENGTH = {3: 150, 4: 360, 5: 480}


def _loop(osm: Osm, lat: float, lon: float, count: int, tags_for, bearing0: float = 0.0, dogleg: int | None = None) -> list[int]:
    """`count` holes laid out round a loop, each tee near the last green."""
    ways = []
    for i in range(count):
        par = PARS_18[i % 18]
        bearing = (bearing0 + i * (360 / count)) % 360
        way, lat, lon = osm.hole(lat, lon, bearing, LENGTH[par], tags_for(i + 1, par), turn=35.0 if dogleg == i + 1 else None)
        lat += 0.0002  # the next tee, a few metres on
        ways.append(way)
    return ways


def single_18() -> None:
    """One 18-hole course, hole 7 a dogleg; holes 5 and 6 have no par."""
    osm = Osm(1)
    osm.area(54.670, 25.260, 54.700, 25.300, {
        "leisure": "golf_course", "name": "Vilkės golfo klubas", "name:lt": "Vilkės golfo klubas",
        "name:en": "Vilke Golf Club", "ele": "160", "website": "https://example.lt",
    }, id=123456)
    def tags(n, par):
        t = {"ref": n, "handicap": n}
        if n not in (5, 6):
            t["par"] = par
        return t
    _loop(osm, 54.676, 25.268, 18, tags, dogleg=7)
    osm.write("single_18.osm")


def club_27() -> None:
    """A 27-hole club: three loops named three ways (golf:course:name, a
    golf=course relation, and hole names "Blå 1"…), inside the club outline,
    plus a 9-hole course outline of its own inside it."""
    osm = Osm(2)
    osm.area(56.240, 12.830, 56.290, 12.900, {"leisure": "golf_course", "name": "Ängelholms GK"}, id=200)
    _loop(osm, 56.246, 12.840, 9, lambda n, p: {"ref": n, "par": p, "golf:course:name": "Röd"})
    gul = _loop(osm, 56.262, 12.840, 9, lambda n, p: {"ref": n, "par": p})
    osm.relation(gul, {"type": "sport", "golf": "course", "name": "Gul"}, id=900)
    _loop(osm, 56.278, 12.840, 9, lambda n, p: {"name": f"Blå {n}", "par": p})
    osm.write("club_27.osm")


def excluded() -> None:
    """Things that are not golf courses, and one too small to count."""
    osm = Osm(3)
    osm.area(54.60, 25.10, 54.602, 25.103, {"leisure": "golf_course", "golf": "driving_range", "name": "Range"}, id=300)
    osm.area(54.61, 25.10, 54.611, 25.101, {"leisure": "miniature_golf", "name": "Mini"}, id=301)
    osm.area(54.62, 25.10, 54.63, 25.12, {"leisure": "golf_course", "sport": "disc_golf", "name": "Disc"}, id=302)
    # About 1 ha, named, no holes: dropped.
    osm.area(54.64, 25.10, 54.6409, 25.1017, {"leisure": "golf_course", "name": "Pitch and putt"}, id=303)
    # A course mapped as a single node: kept.
    osm.node(54.65, 25.15, {"leisure": "golf_course", "name": "Node Golf"}, id=99)
    osm.write("excluded.osm")


def routings() -> None:
    """Two unnamed 9-hole routings sharing numbers, and a 27-hole run 1–27.
    Hole 3 of the first routing has a par 9 (invalid)."""
    osm = Osm(4)
    osm.area(55.000, 24.000, 55.080, 24.100, {"leisure": "golf_course", "name": "Twin Links"}, id=400)
    _loop(osm, 55.005, 24.010, 9, lambda n, p: {"ref": n, "par": 9 if n == 3 else p})
    _loop(osm, 55.040, 24.060, 9, lambda n, p: {"ref": n, "par": p})
    osm.area(56.000, 24.000, 56.080, 24.100, {"leisure": "golf_course", "name": "Long Run"}, id=401)
    _loop(osm, 56.030, 24.010, 27, lambda n, p: {"ref": n, "par": p})
    osm.write("routings.osm")


def northern_ireland() -> None:
    """A course in Belfast, inside Geofabrik's Ireland extract."""
    osm = Osm(5)
    osm.area(54.570, -5.990, 54.590, -5.960, {"leisure": "golf_course", "name": "Belfast Links"}, id=500)
    osm.area(53.330, -6.290, 53.350, -6.260, {"leisure": "golf_course", "name": "Dublin Links"}, id=501)
    osm.write("ireland.osm")


if __name__ == "__main__":
    single_18()
    club_27()
    excluded()
    routings()
    northern_ireland()
