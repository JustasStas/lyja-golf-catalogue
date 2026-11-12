"""Writes the small `.osm` fixtures the tennis tests read:

    python tests/fixtures/make_tennis_fixtures.py

Made-up venues, one file per rule in the brief's Part A test list.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_fixtures import Osm  # noqa: E402


def court(osm: Osm, lat: float, lon: float, tags: dict, bearing: float = 0.0,
          length: float = 36.0, width: float = 18.0, id: int | None = None) -> int:
    """A rectangle `length` along `bearing` and `width` across, centred on
    (lat, lon): one court with its run-off by default."""
    def offset(along: float, across: float) -> tuple[float, float]:
        b = math.radians(bearing)
        north = along * math.cos(b) - across * math.sin(b)
        east = along * math.sin(b) + across * math.cos(b)
        return lat + north / 110_574.0, lon + east / (111_320.0 * math.cos(math.radians(lat)))
    corners = [offset(-length / 2, -width / 2), offset(length / 2, -width / 2), offset(length / 2, width / 2), offset(-length / 2, width / 2)]
    refs = [osm.node(a, b) for a, b in corners]
    return osm.way(refs + [refs[0]], {"leisure": "pitch", **tags}, id)


TENNIS = {"sport": "tennis"}


def park_six() -> None:
    """A named park with six separate hard courts, lit, public: one venue."""
    osm = Osm(11)
    osm.area(54.6830, 25.2930, 54.6860, 25.2980, {"leisure": "park", "name": "Bernardinų sodas"}, id=110)
    for i in range(6):
        court(osm, 54.6845, 25.2940 + i * 0.0004, {**TENNIS, "surface": "hard", "lit": "yes", "access": "yes"}, bearing=172)
    osm.write("tennis_park_six.osm")


def club_mixed() -> None:
    """A sports centre for members with three clay and two hard courts."""
    osm = Osm(12)
    osm.area(54.9000, 23.9260, 54.9020, 23.9300, {"leisure": "sports_centre", "name": "Žalgirio teniso klubas", "website": "https://example.lt"}, id=120)
    for i in range(3):
        court(osm, 54.9006, 23.9265 + i * 0.0004, {**TENNIS, "surface": "clay", "access": "members", "lit": "yes"}, bearing=5)
    court(osm, 54.9014, 23.9265, {**TENNIS, "surface": "hard", "access": "members", "lit": "no"}, bearing=5)
    court(osm, 54.9014, 23.9269, {**TENNIS, "surface": "acrylic", "access": "members"}, bearing=5)
    # A green clay court elsewhere, to check the colour rule.
    court(osm, 54.9100, 23.9500, {**TENNIS, "surface": "clay", "clay:type": "Har-Tru", "name": "Green"}, bearing=5)
    osm.write("tennis_club_mixed.osm")


def multi_court() -> None:
    """Four courts side by side in one pitch, no count tag: 72 m across, 36 m
    long, courts running along the short side (bearing 20)."""
    osm = Osm(13)
    court(osm, 55.0000, 24.0000, {**TENNIS, "name": "Kortai", "surface": "hard"}, bearing=110, length=72, width=36)
    osm.write("tennis_multi_court.osm")


def double_mapping() -> None:
    """A whole-venue tennis pitch drawn around two single courts."""
    osm = Osm(14)
    court(osm, 55.1000, 24.1000, {**TENNIS, "name": "Double"}, bearing=0, length=40, width=50)
    court(osm, 55.1000, 24.0998, {**TENNIS, "name": "Double"}, bearing=0)
    court(osm, 55.1000, 24.1002, {**TENNIS, "name": "Double"}, bearing=0)
    osm.write("tennis_double.osm")


def indoor() -> None:
    """A sports hall around a court, a court tagged indoor and one covered:
    all three dropped. A practice wall and a disused court are left out, a
    padel court is kept for later."""
    osm = Osm(15)
    osm.area(55.2000, 24.2000, 55.2010, 24.2010, {"leisure": "sports_hall", "name": "Hall"}, id=150)
    court(osm, 55.2005, 24.2005, {**TENNIS, "name": "Inside"}, bearing=0)
    court(osm, 55.2100, 24.2100, {**TENNIS, "name": "Indoor", "indoor": "yes"}, bearing=0)
    court(osm, 55.2200, 24.2200, {**TENNIS, "name": "Roofed", "covered": "yes"}, bearing=0)
    court(osm, 55.2300, 24.2300, {**TENNIS, "name": "Wall"}, bearing=0)
    osm.ways[-1] = (osm.ways[-1][0], osm.ways[-1][1].replace('v="pitch"', 'v="practice_pitch"'))
    court(osm, 55.2400, 24.2400, {**TENNIS, "name": "Old", "disused": "yes"}, bearing=0)
    court(osm, 55.2500, 24.2500, {"sport": "padel", "name": "Padel"}, bearing=0, length=20, width=10)
    osm.write("tennis_indoor.osm")


def seasonal() -> None:
    """A court under a seasonal dome: kept and flagged."""
    osm = Osm(16)
    court(osm, 55.3000, 24.3000, {**TENNIS, "name": "Dome", "covered": "seasonal"}, bearing=0)
    osm.write("tennis_seasonal.osm")


def backyard() -> None:
    """One unnamed private court in a residential area: hidden. A named one
    and an unnamed public one beside it stay."""
    osm = Osm(17)
    osm.area(55.4000, 24.4000, 55.4050, 24.4100, {"landuse": "residential"}, id=170)
    court(osm, 55.4010, 24.4010, {**TENNIS, "access": "private"}, bearing=0)
    court(osm, 55.4040, 24.4090, {**TENNIS, "name": "Kiemo kortas"}, bearing=0)
    # Unnamed and public in the same residential area: stays (owner, 2026-11-12).
    court(osm, 55.4025, 24.4050, {**TENNIS, "access": "yes"}, bearing=0)
    osm.write("tennis_backyard.osm")


def road_split() -> None:
    """Two courts either side of a residential street, 30 m apart: two
    venues. Two more 30 m apart with no street between: one venue."""
    osm = Osm(18)
    court(osm, 55.5000, 24.5000, TENNIS, bearing=0)
    court(osm, 55.5000, 24.50075, TENNIS, bearing=0)
    a = osm.node(55.4990, 24.500375)
    b = osm.node(55.5010, 24.500375)
    osm.way([a, b], {"highway": "residential", "name": "Testo gatvė"})
    court(osm, 55.5100, 24.5000, TENNIS, bearing=0)
    court(osm, 55.5100, 24.50075, TENNIS, bearing=0)
    osm.write("tennis_road_split.osm")


def axes() -> None:
    """Single courts north–south, east–west and on the diagonal, far apart."""
    osm = Osm(19)
    court(osm, 55.6000, 24.6000, {**TENNIS, "name": "North south"}, bearing=0)
    court(osm, 55.6100, 24.6100, {**TENNIS, "name": "East west"}, bearing=90)
    court(osm, 55.6200, 24.6200, {**TENNIS, "name": "Diagonal"}, bearing=45)
    osm.write("tennis_axes.osm")


if __name__ == "__main__":
    for make in (park_six, club_mixed, multi_court, double_mapping, indoor, seasonal, backyard, road_split, axes):
        make()
