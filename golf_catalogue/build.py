"""Stage 2, once: merge every region's raw extract into the published files.

    python -m golf_catalogue.build raw/ out/ [--osm-date 2026-10-20]

writes

    out/search-index.json.gz       every course, compact, for search and nearest
    out/courses/{CC}/{id}.json     one course with its layouts and holes
    out/countries/{CC}.json.gz     every course of a country
    out/stats.json                 per-country coverage
    out/build-log.json             counts and the quality notes (not published)

The rules are in the order the spec gives them: courses from the
`leisure=golf_course` outlines (nodes as a fallback), driving ranges, mini and
disc golf and small outlines without holes dropped, each `golf=hole` given to
the smallest course around it (else the nearest within 300 m), holes grouped
into layouts, then validated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import shapely
import shapely.ops
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from . import countries, geo

MIN_COURSE_HA = 3.0
NEAREST_COURSE_M = 300.0
VALID_PAR = range(3, 7)
# Plausible hole length for its par, metres (spec, Part A §3).
PAR_LENGTH_M = {3: (60, 300), 4: (200, 500), 5: (380, 700), 6: (500, 800)}
ANY_LENGTH_M = (50, 800)
HOLE_COUNTS = (9, 18, 27, 36)
ATTRIBUTION = "© OpenStreetMap contributors, ODbL"


# MARK: Holes

@dataclass
class Hole:
    id: str
    ref: int | None
    par: int | None
    hcp: int | None
    coords: list[tuple[float, float]]  # (lat, lon)
    loop: str | None  # golf:course:name, or the "Blå" of a hole named "Blå 1"
    length_m: float
    bearing_tee: float
    bearing_approach: float
    issue: str | None = None

    @property
    def tee(self) -> tuple[float, float]:
        return self.coords[0]

    @property
    def green(self) -> tuple[float, float]:
        return self.coords[-1]


_INT = re.compile(r"\d+")
_LOOP_NAME = re.compile(r"^(?P<loop>.*\D)\s*(?P<n>\d{1,2})$")


def _int(value: str | None) -> int | None:
    if not value:
        return None
    match = _INT.search(value)
    return int(match.group()) if match else None


def parse_hole(raw: dict) -> Hole:
    tags = raw["tags"]
    coords = [(c[0], c[1]) for c in raw["coords"]]
    ref = _int(tags.get("ref"))
    loop = tags.get("golf:course:name") or tags.get("golf:course")
    name = (tags.get("name") or "").strip()
    named = _LOOP_NAME.match(name)
    if named and not named.group("loop").strip().isdigit():
        if ref is None:
            ref = int(named.group("n"))
        loop = loop or named.group("loop").strip(" -–") or None
    hole = Hole(
        id=raw["id"],
        ref=ref,
        par=_int(tags.get("par")),
        hcp=_int(tags.get("handicap")),
        coords=coords,
        loop=loop,
        length_m=geo.path_length_m(coords),
        bearing_tee=geo.initial_bearing(*coords[0], *coords[1]),
        bearing_approach=geo.initial_bearing(*coords[-2], *coords[-1]),
    )
    hole.issue = validate_hole(hole)
    return hole


def validate_hole(hole: Hole) -> str | None:
    """Why a hole cannot be used, or None."""
    if hole.ref is None or not 1 <= hole.ref <= 36:
        return "no hole number"
    if hole.par is not None and hole.par not in VALID_PAR:
        return f"par {hole.par} outside 3–6"
    low, high = PAR_LENGTH_M.get(hole.par, ANY_LENGTH_M)
    if not low <= hole.length_m <= high:
        return f"length {hole.length_m:.0f} m implausible for par {hole.par or '?'}"
    return None


# MARK: Courses

@dataclass
class Course:
    id: str
    tags: dict[str, str]
    country: str
    geom: object | None  # shapely (Multi)Polygon in lon/lat, or None for a node
    point: tuple[float, float]  # (lat, lon) centroid
    area_ha: float
    holes: list[tuple[Hole, str | None]] = field(default_factory=list)  # (hole, layout hint)
    parent: "Course | None" = None

    @property
    def name(self) -> str | None:
        return (self.tags.get("name") or self.tags.get("name:en") or self.tags.get("golf:course:name") or "").strip() or None

    @property
    def has_course_tags(self) -> bool:
        return any(k.startswith("golf:course") for k in self.tags)


def _load_raw(directory: Path) -> list[dict]:
    raws = []
    for path in sorted(directory.rglob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raws.append(json.load(f))
    return raws


def _courses_from_raw(raws: list[dict]) -> dict[str, Course]:
    courses: dict[str, Course] = {}
    for raw in raws:
        for c in raw["courses"]:
            if c["id"] in courses:  # the same course in two overlapping extracts
                continue
            if "wkt" in c:
                geom = shapely.from_wkt(c["wkt"])
                if geom.is_empty:
                    continue
                if not geom.is_valid:
                    geom = shapely.make_valid(geom)
                center = geom.representative_point() if not geom.centroid.within(geom) else geom.centroid
                lat, lon = center.y, center.x
                area = geo.geometry_area_ha(geom)
            else:
                geom = None
                lat, lon = c["point"]
                area = 0.0
            courses[c["id"]] = Course(
                id=c["id"], tags=c["tags"], country=countries.resolve(raw["country"], c["tags"], lat, lon),
                geom=geom, point=(lat, lon), area_ha=area,
            )
    return courses


def _nest_clubs(courses: dict[str, Course]) -> None:
    """A course outline inside a bigger one is a course of that club: its
    holes become a layout of the club's entry."""
    polys = [c for c in courses.values() if c.geom is not None]
    tree = STRtree([c.geom for c in polys])
    for course in polys:
        best: Course | None = None
        for i in tree.query(course.geom, predicate="within"):
            other = polys[i]
            if other is course or other.area_ha <= course.area_ha:
                continue
            if best is None or other.area_ha < best.area_ha:
                best = other
        course.parent = best


def _assign_holes(courses: dict[str, Course], holes: list[Hole], group_of: dict[str, str]) -> list[dict]:
    """Each hole to the smallest course outline holding it, else the nearest
    course within 300 m. Returns the holes no course took, as notes."""
    polys = [c for c in courses.values() if c.geom is not None]
    tree = STRtree([c.geom for c in polys])
    nodes = [c for c in courses.values() if c.geom is None]
    unassigned = []
    for hole in holes:
        tee = Point(hole.tee[1], hole.tee[0])
        line = LineString([(lon, lat) for lat, lon in hole.coords])
        mid = line.interpolate(0.5, normalized=True)
        holders = [polys[i] for i in tree.query(line) if polys[i].geom.covers(tee) or polys[i].geom.covers(mid)]
        target = min(holders, key=lambda c: c.area_ha) if holders else None
        if target is None:
            target = _nearest(hole, polys, tree, nodes)
        if target is None:
            unassigned.append({"hole": hole.id, "issue": "no course within 300 m"})
            continue
        target.holes.append((hole, group_of.get(hole.id)))
    return unassigned


def _nearest(hole: Hole, polys: list[Course], tree: STRtree, nodes: list[Course]) -> Course | None:
    lat, lon = hole.tee
    reach = geo.metres_to_degrees(NEAREST_COURSE_M, lat)
    best, best_m = None, NEAREST_COURSE_M
    tee = Point(lon, lat)
    for i in tree.query(tee.buffer(reach)):
        course = polys[i]
        near = shapely.ops.nearest_points(course.geom, tee)[0]
        d = geo.haversine_m(lat, lon, near.y, near.x)
        if d <= best_m:
            best, best_m = course, d
    for course in nodes:
        d = geo.haversine_m(lat, lon, *course.point)
        if d <= best_m:
            best, best_m = course, d
    return best


# MARK: Layouts

@dataclass
class Layout:
    name: str
    holes: list[Hole]

    @property
    def numbers(self) -> list[int]:
        return sorted(h.ref for h in self.holes)

    @property
    def complete(self) -> bool:
        return self.numbers in (list(range(1, 10)), list(range(1, 19)))


def build_layouts(course: Course, notes: list[dict]) -> list[Layout]:
    """Group a course's valid holes into layouts: by course relation, then
    `golf:course:name` / named loops, then by hole number."""
    valid: list[tuple[Hole, str | None]] = []
    for hole, hint in course.holes:
        if hole.issue:
            notes.append({"course": course.id, "hole": hole.id, "issue": hole.issue})
        else:
            valid.append((hole, hint))

    keyed: dict[str, list[Hole]] = defaultdict(list)
    loose: list[Hole] = []
    for hole, hint in valid:
        key = hint or hole.loop
        (keyed[key].append(hole) if key else loose.append(hole))

    layouts = [Layout(name, holes) for name, holes in sorted(keyed.items())]
    if loose:
        layouts += _split_by_numbers(loose, course.name or "Main")

    for layout in layouts:
        layout.holes = _dedupe(layout, course, notes)
    return [layout for layout in layouts if layout.holes]


def _split_by_numbers(holes: list[Hole], default_name: str) -> list[Layout]:
    refs = [h.ref for h in holes]
    if len(refs) == len(set(refs)):
        ordered = sorted(holes, key=lambda h: h.ref)
        top = max(refs)
        if top in (27, 36) and sorted(refs) == list(range(1, top + 1)):
            # One run of 27 or 36: loops of nine, renumbered 1–9.
            loops = []
            for start in range(1, top + 1, 9):
                loop = [h for h in ordered if start <= h.ref < start + 9]
                for h in loop:
                    h.ref -= start - 1
                loops.append(Layout(f"Holes {start}–{start + 8}", loop))
            return loops
        return [Layout(default_name, ordered)]
    # Repeated numbers: several routings drawn without names. Walk the
    # numbers in order and give each hole to the routing whose last green is
    # nearest its tee.
    routings: list[list[Hole]] = []
    for ref in sorted(set(refs)):
        for hole in sorted((h for h in holes if h.ref == ref), key=lambda h: h.id):
            open_ = [r for r in routings if all(h.ref != ref for h in r)]
            if not open_:
                routings.append([hole])
                continue
            best = min(open_, key=lambda r: geo.haversine_m(*r[-1].green, *hole.tee))
            best.append(hole)
    names = [default_name] if len(routings) == 1 else [f"{default_name} {i + 1}" for i in range(len(routings))]
    return [Layout(name, routing) for name, routing in zip(names, routings)]


def _dedupe(layout: Layout, course: Course, notes: list[dict]) -> list[Hole]:
    seen: set[int] = set()
    kept = []
    for hole in sorted(layout.holes, key=lambda h: (h.ref, h.id)):
        if hole.ref in seen:
            notes.append({"course": course.id, "hole": hole.id, "issue": f"duplicate hole {hole.ref} in {layout.name}"})
            continue
        seen.add(hole.ref)
        kept.append(hole)
    return kept


# MARK: Output

def _hole_json(hole: Hole) -> dict:
    out = {"n": hole.ref}
    if hole.par is not None:
        out["par"] = hole.par
    if hole.hcp is not None:
        out["hcp"] = hole.hcp
    out.update({
        "tee": [round(hole.tee[0], 6), round(hole.tee[1], 6)],
        "green": [round(hole.green[0], 6), round(hole.green[1], 6)],
        "bearing_tee": round(hole.bearing_tee, 1),
        "bearing_approach": round(hole.bearing_approach, 1),
        "length_m": round(hole.length_m),
    })
    return out


def hole_count(course: Course, layouts: list[Layout]) -> int:
    complete = sum(len(l.holes) for l in layouts if l.complete)
    if complete in HOLE_COUNTS:
        return complete
    tagged = _int(course.tags.get("golf:holes") or course.tags.get("holes"))
    if tagged in HOLE_COUNTS:
        return tagged
    total = sum(len(l.holes) for l in layouts)
    return total if total in HOLE_COUNTS else 0


def course_json(course: Course, layouts: list[Layout], osm_date: str) -> dict:
    tags = course.tags
    names = {k.split(":", 1)[1]: v for k, v in tags.items() if re.fullmatch(r"name:[a-z]{2,3}", k)}
    out = {
        "id": course.id,
        "name": course.name,
        "names": names,
        "country": course.country,
        "centroid": [round(course.point[0], 5), round(course.point[1], 5)],
    }
    if course.geom is not None:
        minx, miny, maxx, maxy = course.geom.bounds
        out["bbox"] = [round(miny, 5), round(minx, 5), round(maxy, 5), round(maxx, 5)]
    ele = tags.get("ele")
    try:
        if ele:
            out["elevation_m"] = round(float(ele.split()[0].replace(",", ".")))
    except ValueError:
        pass
    website = tags.get("website") or tags.get("contact:website")
    if website:
        out["website"] = website
    out["layouts"] = [
        {"name": l.name, "holes": [_hole_json(h) for h in sorted(l.holes, key=lambda h: h.ref)], "complete": l.complete}
        for l in layouts
    ]
    out["osm_version_date"] = osm_date
    out["attribution"] = ATTRIBUTION
    return out


def index_entry(course_out: dict, holes: int) -> dict:
    return {
        "id": course_out["id"],
        "n": course_out["name"],
        "c": course_out["country"],
        "la": course_out["centroid"][0],
        "lo": course_out["centroid"][1],
        "h": holes,
        "g": 1 if any(l["complete"] for l in course_out["layouts"]) else 0,
    }


def build(raw_dir: Path, out_dir: Path, osm_date: str) -> dict:
    raws = _load_raw(raw_dir)
    # The oldest region's data date: the whole build is at least this fresh.
    stamps = sorted(r["timestamp"][:10] for r in raws if r.get("timestamp"))
    if stamps:
        osm_date = stamps[0]
    courses = _courses_from_raw(raws)
    holes_raw: dict[str, dict] = {}
    group_of: dict[str, str] = {}
    for raw in raws:
        for h in raw["holes"]:
            holes_raw.setdefault(h["id"], h)
        for g in raw["groups"]:
            name = g["tags"].get("name") or g["tags"].get("ref") or g["id"]
            for member in g["members"]:
                group_of.setdefault(member, name)
    holes = [parse_hole(h) for h in holes_raw.values()]

    _nest_clubs(courses)
    notes: list[dict] = _assign_holes(courses, holes, group_of)

    # A course inside a club hands its holes to the club, named after it.
    for course in courses.values():
        if course.parent is not None:
            root = course.parent
            while root.parent is not None:
                root = root.parent
            root.holes += [(hole, hint or course.name or course.id) for hole, hint in course.holes]
            course.holes = []

    out_dir.mkdir(parents=True, exist_ok=True)
    index: list[dict] = []
    by_country: dict[str, list[dict]] = defaultdict(list)
    stats: dict[str, dict] = defaultdict(lambda: {"courses": 0, "with_holes": 0, "complete_9": 0, "complete_18": 0})
    dropped = defaultdict(int)
    for course in sorted(courses.values(), key=lambda c: c.id):
        if course.parent is not None:
            dropped["inside a club"] += 1
            continue
        if course.name is None and not course.has_course_tags:
            dropped["unnamed"] += 1
            continue
        if course.geom is not None and course.area_ha < MIN_COURSE_HA and not course.holes:
            dropped["under 3 ha without holes"] += 1
            continue
        layouts = build_layouts(course, notes)
        out = course_json(course, layouts, osm_date)
        holes_n = hole_count(course, layouts)
        entry = index_entry(out, holes_n)
        index.append(entry)
        by_country[course.country].append(out)
        s = stats[course.country]
        s["courses"] += 1
        s["with_holes"] += 1 if layouts else 0
        s["complete_9"] += 1 if any(l.complete and len(l.holes) == 9 for l in layouts) else 0
        s["complete_18"] += 1 if any(l.complete and len(l.holes) == 18 for l in layouts) else 0
        path = out_dir / "courses" / course.country / f"{course.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    with gzip.open(out_dir / "search-index.json.gz", "wt", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "countries").mkdir(exist_ok=True)
    for country, items in by_country.items():
        with gzip.open(out_dir / "countries" / f"{country}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, separators=(",", ":"))

    totals = {k: sum(s[k] for s in stats.values()) for k in ("courses", "with_holes", "complete_9", "complete_18")}
    stats_out = {
        "osm_version_date": osm_date,
        "totals": totals,
        "countries": {c: stats[c] for c in sorted(stats)},
    }
    (out_dir / "stats.json").write_text(json.dumps(stats_out, ensure_ascii=False, indent=1), encoding="utf-8")
    log = {
        "regions": [r["region"] for r in raws],
        "holes_in": len(holes),
        "dropped": dict(dropped),
        "quality": notes,
    }
    (out_dir / "build-log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge region extracts into the golf catalogue.")
    parser.add_argument("raw_dir")
    parser.add_argument("out_dir")
    parser.add_argument("--osm-date", default=dt.date.today().isoformat())
    args = parser.parse_args(argv)
    stats = build(Path(args.raw_dir), Path(args.out_dir), args.osm_date)
    t = stats["totals"]
    print(f"{t['courses']} courses, {t['with_holes']} with holes, {t['complete_18']} complete 18s, {t['complete_9']} complete 9s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
