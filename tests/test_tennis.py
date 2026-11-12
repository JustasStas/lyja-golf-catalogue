"""The tennis venue catalogue's rules on small made-up venues
(tests/fixtures/tennis_*.osm, from make_tennis_fixtures.py)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import jsonschema
import pytest

from tennis_catalogue import boxes, build, extract, guard, publish

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = Path(__file__).parent.parent / "schema"


def run(tmp_path: Path, *names: str, country: str = "LT") -> tuple[dict, Path]:
    raw = tmp_path / "raw-tennis"
    raw.mkdir()
    for i, name in enumerate(names):
        path = FIXTURES / f"tennis_{name}.osm"
        doc = extract.extract([path, path], f"{name}-{i}", country, "2026-10-28T20:21:02Z")
        with gzip.open(raw / f"{name}-{i}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(doc, f)
    out = tmp_path / "out-tennis"
    build.build(raw, out, "2026-10-28")
    venues = {}
    for path in (out / "venues").rglob("*.json"):
        venue = json.loads(path.read_text(encoding="utf-8"))
        venues[venue["id"]] = venue
    return venues, out


def index(out: Path) -> dict:
    with gzip.open(out / "search-index.json.gz", "rt", encoding="utf-8") as f:
        return {e["id"]: e for e in json.load(f)}


def stats(out: Path) -> dict:
    return json.loads((out / "stats.json").read_text(encoding="utf-8"))


def by_name(venues: dict) -> dict:
    return {v["name"]: v for v in venues.values()}


# MARK: Venues

def test_park_with_six_courts_is_one_venue(tmp_path):
    venues, out = run(tmp_path, "park_six")
    [venue] = venues.values()
    assert venue["id"] == "w110" and venue["name"] == "Bernardinų sodas" and not venue["name_generated"]
    assert venue["courts_est"] == 6
    assert venue["surfaces"]["hard"] == 6 and venue["surfaces"]["unknown"] == 0
    assert venue["lit"] == "yes" and venue["access"] == "public"
    assert venue["axis_confidence"] == "high" and abs(venue["axis_deg"] - 172) < 1.5
    entry = index(out)["w110"]
    assert entry == {"id": "w110", "n": "Bernardinų sodas", "c": "LT", "la": entry["la"], "lo": entry["lo"], "k": 6, "s": "h", "l": 1, "a": "p"}


def test_club_with_clay_and_hard_courts(tmp_path):
    venues, out = run(tmp_path, "club_mixed")
    club = by_name(venues)["Žalgirio teniso klubas"]
    assert club["id"] == "w120" and club["courts_est"] == 5
    assert club["surfaces"]["red_clay"] == 3 and club["surfaces"]["hard"] == 2
    assert club["access"] == "customers" and club["lit"] == "partial"
    assert club["website"] == "https://example.lt"
    entry = index(out)["w120"]
    assert entry["s"] == "rc" and entry["a"] == "c" and entry["l"] == 1
    green = by_name(venues)["Green"]
    assert green["surfaces"]["green_clay"] == 1


def test_multi_court_pitch_counts_courts_from_its_area(tmp_path):
    venues, _ = run(tmp_path, "multi_court")
    [venue] = venues.values()
    assert venue["courts_est"] == 4
    assert venue["axis_confidence"] == "low"
    assert build._axial_diff(venue["axis_deg"], 20) < 1.5, "side by side: the courts run along the short side"


def test_double_mapping_drops_the_outer_pitch(tmp_path):
    venues, out = run(tmp_path, "double")
    [venue] = venues.values()
    assert venue["courts_est"] == 2
    assert stats(out)["countries"]["LT"]["double_mapped_dropped"] == 1


def test_indoor_dropped_and_others_left_out(tmp_path):
    venues, out = run(tmp_path, "indoor")
    assert venues == {}
    s = stats(out)["countries"]["LT"]
    assert s["indoor_dropped"] == 3
    assert s["practice_walls"] == 1
    assert s["disused_dropped"] == 1
    with gzip.open(out / "later" / "padel.json.gz", "rt", encoding="utf-8") as f:
        assert [v["name"] for v in json.load(f)] == ["Padel"]
    assert "later/padel.json.gz" not in {p.relative_to(out).as_posix() for p in publish.published_paths(out)}


def test_seasonal_dome_is_kept_and_flagged(tmp_path):
    venues, _ = run(tmp_path, "seasonal")
    [venue] = venues.values()
    assert venue["covered_share"] == 1.0


def test_backyard_court_is_hidden(tmp_path):
    venues, out = run(tmp_path, "backyard")
    assert [v["name"] for v in venues.values()] == ["Kiemo kortas"]
    assert stats(out)["countries"]["LT"]["backyard_hidden"] == 1
    assert len(index(out)) == 1


def test_a_street_between_courts_makes_two_venues(tmp_path):
    venues, _ = run(tmp_path, "road_split")
    courts = sorted(v["courts_est"] for v in venues.values())
    assert courts == [1, 1, 2]
    split = [v for v in venues.values() if v["courts_est"] == 1]
    assert all(v["name_generated"] and v["name"] == "Tennis courts, Testo gatvė" for v in split)
    assert all(v["name_base"] == "Testo gatvė" for v in split)


@pytest.mark.parametrize("name, axis", [("North south", 0), ("East west", 90), ("Diagonal", 45)])
def test_axis(tmp_path, name, axis):
    venues, _ = run(tmp_path, "axes")
    venue = by_name(venues)[name]
    assert venue["axis_confidence"] == "high"
    assert build._axial_diff(venue["axis_deg"], axis) < 1.5


def test_venue_axis_disagreement_is_none():
    def p(axis, courts=1, high=True):
        return build.Pitch(id="w1", tags={}, geom=None, lat=0, lon=0, country="LT", courts=courts, axis=axis, axis_high=high)
    assert build.venue_axis([p(10), p(170)])[1] == "high", "10° and 170° are 20° apart"
    assert build.venue_axis([p(0), p(60)]) == (None, "none")
    assert build.venue_axis([p(0, high=False), p(10)])[1] == "low"


def test_surface_mapping():
    assert build.surface_of({"surface": "clay"}) == "red_clay"
    assert build.surface_of({"surface": "clay", "surface:colour": "green"}) == "green_clay"
    for s in ("hard", "acrylic", "asphalt", "concrete"):
        assert build.surface_of({"surface": s}) == "hard"
    assert build.surface_of({"surface": "artificial_turf"}) == "artificial_grass"
    assert build.surface_of({"surface": "grass"}) == "grass"
    assert build.surface_of({"surface": "carpet"}) == "unknown"
    assert build.surface_of({}) == "unknown"


def test_court_count_rules():
    assert build.court_count({"courts": "3"}, 400, 3000, False) == 3
    assert build.court_count({"pitch:count": "2"}, 400, 3000, False) == 2
    assert build.court_count({}, 108, 648, False) == 1
    assert build.court_count({}, 216, 2592, False) == 4
    assert build.court_count({}, 0, 0, True) == 1


# MARK: Files, guard, boxes

def test_every_file_matches_its_schema(tmp_path):
    _, out = run(tmp_path, "park_six", "club_mixed", "multi_court", "double", "indoor", "seasonal", "backyard", "road_split", "axes")
    load = lambda name: json.loads((SCHEMA / name).read_text(encoding="utf-8"))  # noqa: E731
    with gzip.open(out / "search-index.json.gz", "rt", encoding="utf-8") as f:
        jsonschema.validate(json.load(f), load("tennis-search-index.schema.json"))
    for path in (out / "venues").rglob("*.json"):
        jsonschema.validate(json.loads(path.read_text(encoding="utf-8")), load("tennis-venue.schema.json"))
    jsonschema.validate(stats(out), load("tennis-stats.schema.json"))
    publish.main([str(out), "--build-id", "test", "--manifest-only"])
    jsonschema.validate(json.loads((out / "manifest.json").read_text(encoding="utf-8")), load("tennis-manifest.schema.json"))
    s = stats(out)
    assert set(s["countries"]["LT"]) >= {"raw_objects", "pitches_kept", "indoor_dropped", "backyard_hidden", "venues",
                                         "pct_with_surface", "pct_lit_known", "pct_axis_high"}


def test_guard():
    new = {"totals": {"venues": 90}, "countries": {"LT": {"venues": 15}, "LV": {"venues": 30}}}
    old = {"totals": {"venues": 100}, "countries": {"LT": {"venues": 20}, "LV": {"venues": 30}, "EE": {"venues": 5}}}
    problems = guard.check(new, old)
    assert any(p.startswith("world") for p in problems) and any(p.startswith("LT") for p in problems)
    assert guard.check(new, None) == []
    assert guard.check(new, None, index_bytes=7 * 1_048_576)


def test_boxes_cover_the_pitches_only():
    area = boxes.boxes(FIXTURES / "tennis_road_split.osm")
    assert area is not None and area.covers(boxes.shapely.geometry.Point(24.5, 55.5))
    assert not area.covers(boxes.shapely.geometry.Point(24.6, 55.6))
    assert boxes.boxes(FIXTURES / "excluded.osm") is None
