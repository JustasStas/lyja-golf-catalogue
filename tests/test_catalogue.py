"""The catalogue's rules on small made-up courses (tests/fixtures)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import jsonschema
import pytest

from golf_catalogue import build, extract, geo, guard, publish

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = Path(__file__).parent.parent / "schema"


def run(tmp_path: Path, *fixtures: tuple[str, str]) -> tuple[dict, Path]:
    """Extract each (fixture, country) as its own region, build, return
    (courses by id, out dir)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i, (name, country) in enumerate(fixtures):
        doc = extract.extract(FIXTURES / f"{name}.osm", f"{name}-{i}", country)
        with gzip.open(raw / f"{name}-{i}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(doc, f)
    out = tmp_path / "out"
    build.build(raw, out, "2026-10-20")
    courses = {}
    for path in (out / "courses").rglob("*.json"):
        course = json.loads(path.read_text(encoding="utf-8"))
        courses[course["id"]] = course
    return courses, out


def index(out: Path) -> dict:
    with gzip.open(out / "search-index.json.gz", "rt", encoding="utf-8") as f:
        return {e["id"]: e for e in json.load(f)}


# MARK: Courses

def test_single_18(tmp_path):
    courses, out = run(tmp_path, ("single_18", "LT"))
    course = courses["w123456"]
    assert course["name"] == "Vilkės golfo klubas"
    assert course["names"] == {"lt": "Vilkės golfo klubas", "en": "Vilke Golf Club"}
    assert course["country"] == "LT"
    assert course["elevation_m"] == 160
    assert course["website"] == "https://example.lt"
    [layout] = course["layouts"]
    assert layout["complete"] is True
    assert [h["n"] for h in layout["holes"]] == list(range(1, 19))
    entry = index(out)["w123456"]
    assert entry == {"id": "w123456", "n": "Vilkės golfo klubas", "c": "LT", "la": entry["la"], "lo": entry["lo"], "h": 18, "g": 1}
    assert 54.67 < entry["la"] < 54.70 and 25.26 < entry["lo"] < 25.30


def test_missing_pars_are_kept(tmp_path):
    courses, _ = run(tmp_path, ("single_18", "LT"))
    holes = {h["n"]: h for h in courses["w123456"]["layouts"][0]["holes"]}
    assert "par" not in holes[5] and "par" not in holes[6]
    assert holes[4]["par"] == 5 and holes[4]["hcp"] == 4


def test_dogleg_bearings_differ(tmp_path):
    courses, _ = run(tmp_path, ("single_18", "LT"))
    holes = {h["n"]: h for h in courses["w123456"]["layouts"][0]["holes"]}
    dogleg, straight = holes[7], holes[8]
    assert abs(dogleg["bearing_approach"] - dogleg["bearing_tee"]) == pytest.approx(35.0, abs=0.5)
    assert straight["bearing_approach"] == pytest.approx(straight["bearing_tee"], abs=0.1)
    assert dogleg["length_m"] == pytest.approx(150, abs=3)  # par 3 at 150 m


def test_club_with_three_loops(tmp_path):
    courses, out = run(tmp_path, ("club_27", "SE"))
    club = courses["w200"]
    layouts = {l["name"]: l for l in club["layouts"]}
    assert set(layouts) == {"Röd", "Gul", "Blå"}
    for layout in layouts.values():
        assert layout["complete"] and [h["n"] for h in layout["holes"]] == list(range(1, 10))
    entry = index(out)["w200"]
    assert entry["h"] == 27 and entry["g"] == 1


def test_routings_and_long_runs(tmp_path):
    courses, out = run(tmp_path, ("routings", "LT"))
    twin = courses["w400"]
    assert len(twin["layouts"]) == 2
    first, second = sorted(twin["layouts"], key=lambda l: l["name"])
    # Hole 3 (par 9) was dropped, so the first routing is incomplete.
    assert [h["n"] for h in first["holes"]] == [1, 2, 4, 5, 6, 7, 8, 9] and not first["complete"]
    assert second["complete"]
    log = json.loads((out / "build-log.json").read_text())
    assert any(note["issue"].startswith("par 9") for note in log["quality"])
    run_27 = courses["w401"]
    assert [l["name"] for l in run_27["layouts"]] == ["Holes 1–9", "Holes 10–18", "Holes 19–27"]
    assert all(l["complete"] for l in run_27["layouts"])
    assert index(out)["w401"]["h"] == 27


def test_exclusions(tmp_path):
    courses, out = run(tmp_path, ("excluded", "LT"))
    # Range, mini golf and disc golf never get in; the 1 ha outline without
    # holes is dropped; the node course stays.
    assert set(courses) == {"n99"}
    assert index(out)["n99"]["h"] == 0
    log = json.loads((out / "build-log.json").read_text())
    assert log["dropped"] == {"under 3 ha without holes": 1}


def test_course_in_two_regions_counts_once(tmp_path):
    courses, out = run(tmp_path, ("single_18", "LT"), ("single_18", "LV"))
    assert list(courses) == ["w123456"] and courses["w123456"]["country"] == "LT"
    stats = json.loads((out / "stats.json").read_text())
    assert stats["totals"]["courses"] == 1 and "LV" not in stats["countries"]


def test_northern_ireland_is_gb(tmp_path):
    courses, _ = run(tmp_path, ("ireland", "IE"))
    assert courses["w500"]["country"] == "GB"
    assert courses["w501"]["country"] == "IE"


def test_stats(tmp_path):
    _, out = run(tmp_path, ("single_18", "LT"), ("club_27", "SE"), ("routings", "LT"))
    stats = json.loads((out / "stats.json").read_text())
    # Vilkės (18), Twin Links (one complete 9) and Long Run (three 9s).
    assert stats["countries"]["LT"] == {"courses": 3, "with_holes": 3, "complete_9": 2, "complete_18": 1}
    assert stats["countries"]["SE"] == {"courses": 1, "with_holes": 1, "complete_9": 1, "complete_18": 0}
    assert stats["totals"]["courses"] == 4


# MARK: Schemas

def test_every_file_validates(tmp_path):
    _, out = run(tmp_path, ("single_18", "LT"), ("club_27", "SE"), ("routings", "LT"), ("excluded", "LT"), ("ireland", "IE"))
    course_schema = json.loads((SCHEMA / "course.schema.json").read_text())
    for path in (out / "courses").rglob("*.json"):
        jsonschema.validate(json.loads(path.read_text()), course_schema)
    for path in (out / "countries").glob("*.json.gz"):
        with gzip.open(path, "rt") as f:
            for course in json.load(f):
                jsonschema.validate(course, course_schema)
    with gzip.open(out / "search-index.json.gz", "rt") as f:
        jsonschema.validate(json.load(f), json.loads((SCHEMA / "search-index.schema.json").read_text()))
    stats = json.loads((out / "stats.json").read_text())
    doc = publish.manifest(out, "2026-11-01T0300Z", stats)
    jsonschema.validate(doc, json.loads((SCHEMA / "manifest.schema.json").read_text()))
    assert doc["base"] == "builds/2026-11-01T0300Z/"
    assert [f["path"] for f in doc["files"]] == ["search-index.json.gz", "stats.json"]


# MARK: Guard

def counts(world: int, **countries: int) -> dict:
    return {"totals": {"courses": world}, "countries": {c: {"courses": n} for c, n in countries.items()}}


def test_guard_passes_and_trips():
    before = counts(1000, DE=700, LT=40, MT=4)
    assert guard.check(counts(990, DE=690, LT=40, MT=4), before) == []
    assert guard.check(counts(990, DE=690, LT=40, MT=4), None) == []
    # A country losing more than 20 %.
    assert guard.check(counts(990, DE=690, LT=31, MT=4), before) == ["LT: 40 → 31 courses (22.5% fewer)"]
    # The world losing more than 5 %, and a country gone entirely.
    problems = guard.check(counts(940, LT=40, MT=4), before)
    assert problems[0].startswith("world: 1000 → 940") and problems[1].startswith("DE: 700 → 0")
    # Small countries move by whole courses: only the world total counts.
    assert guard.check(counts(997, DE=700, LT=40, MT=1), before) == []


def test_guard_cli_refuses(tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(json.dumps({**counts(10, DE=5), "osm_version_date": "2026-10-20"}))
    previous = tmp_path / "manifest.json"
    previous.write_text(json.dumps({"counts": counts(100, DE=90)}))
    assert guard.main([str(stats), "--previous", str(previous)]) == 1
    assert guard.main([str(stats), "--previous", str(tmp_path / "missing.json")]) == 0


# MARK: Geometry

@pytest.mark.parametrize(("to", "expected"), [
    ((1.0, 0.0), 0.0),      # north
    ((0.0, 1.0), 90.0),     # east
    ((-1.0, 0.0), 180.0),   # south
    ((0.0, -1.0), 270.0),   # west
    ((1.0, 1.0), 45.0),     # NE
    ((-1.0, 1.0), 135.0),   # SE
    ((-1.0, -1.0), 225.0),  # SW
    ((1.0, -1.0), 315.0),   # NW
])
def test_bearing_quadrants(to, expected):
    assert geo.initial_bearing(0.0, 0.0, *to) == pytest.approx(expected, abs=0.05)


def test_bearing_across_the_antimeridian():
    # Fiji-ish: a short hop east over 180° is east, not west round the world.
    assert geo.initial_bearing(-17.0, 179.999, -17.0, -179.999) == pytest.approx(90.0, abs=0.01)
    assert geo.initial_bearing(-17.0, -179.999, -17.0, 179.999) == pytest.approx(270.0, abs=0.01)
    assert geo.haversine_m(-17.0, 179.999, -17.0, -179.999) == pytest.approx(213, abs=2)


def test_area_of_a_hectare():
    from shapely.geometry import box
    lat = 54.0
    side_lat = 100 / 111_195.0
    side_lon = 100 / (111_195.0 * __import__("math").cos(__import__("math").radians(lat)))
    assert geo.geometry_area_ha(box(25.0, lat, 25.0 + side_lon, lat + side_lat)) == pytest.approx(1.0, rel=0.01)
