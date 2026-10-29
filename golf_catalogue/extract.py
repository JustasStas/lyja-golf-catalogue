"""Stage 1, per region: read a filtered OSM file and keep only what the
catalogue needs.

Input is the output of
`osmium tags-filter region.osm.pbf nwr/leisure=golf_course nwr/golf r/type=sport`
(any OSM format pyosmium reads; the tests use small `.osm` XML files).

Output is one gzipped JSON document per region:

    {"region": "lithuania", "country": "LT",
     "courses": [{"id": "w1", "tags": {...}, "wkt": "MULTIPOLYGON(...)"} |
                 {"id": "n7", "tags": {...}, "point": [lat, lon]}],
     "holes":   [{"id": "w5", "tags": {...}, "coords": [[lat, lon], ...]}],
     "groups":  [{"id": "r9", "tags": {...}, "members": ["w5", "w6"]}]}

Nothing here decides what a course *is* beyond the OSM tags: assignment,
layouts and validation happen in `build`, once every region is in.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

import osmium
import shapely.wkb

# Tags worth carrying to the build. Everything else is dropped here so the
# per-region artifacts stay small.
COURSE_TAG_PREFIXES = ("name", "golf", "ele", "website", "contact:website", "holes", "sport", "addr:country", "leisure")
HOLE_TAG_KEYS = ("golf", "ref", "par", "handicap", "name", "golf:course:name", "golf:course", "dist")


def _keep(tags, prefixes) -> dict[str, str]:
    return {t.k: t.v for t in tags if t.k.startswith(prefixes)}


def is_excluded(tags: dict[str, str]) -> bool:
    """Driving ranges, mini golf and disc golf are not golf courses."""
    return (
        tags.get("golf") == "driving_range"
        or tags.get("leisure") == "miniature_golf"
        or "disc_golf" in tags.get("sport", "")
        or tags.get("leisure") == "disc_golf_course"
    )


def extract(path: str | Path, region: str, country: str, timestamp: str | None = None) -> dict:
    courses: dict[str, dict] = {}
    holes: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    wkb = osmium.geom.WKBFactory()

    processor = osmium.FileProcessor(str(path)).with_locations().with_areas()
    for obj in processor:
        if obj.is_area():
            tags = {t.k: t.v for t in obj.tags}
            if tags.get("leisure") != "golf_course" or is_excluded(tags):
                continue
            try:
                geom = shapely.wkb.loads(wkb.create_multipolygon(obj), hex=True)
            except Exception:  # broken multipolygon in OSM: skip, the build logs counts
                continue
            oid = ("w" if obj.from_way() else "r") + str(obj.orig_id())
            courses[oid] = {
                "id": oid,
                "tags": _keep(obj.tags, COURSE_TAG_PREFIXES),
                "wkt": shapely.to_wkt(geom, rounding_precision=7),
            }
        elif obj.is_node():
            tags = obj.tags
            if tags.get("leisure") == "golf_course" and obj.location.valid():
                kept = _keep(tags, COURSE_TAG_PREFIXES)
                if is_excluded(kept):
                    continue
                oid = "n" + str(obj.id)
                courses[oid] = {"id": oid, "tags": kept, "point": [round(obj.location.lat, 7), round(obj.location.lon, 7)]}
        elif obj.is_way():
            if obj.tags.get("golf") != "hole":
                continue
            coords = [[round(n.lat, 7), round(n.lon, 7)] for n in obj.nodes if n.location.valid()]
            if len(coords) < 2:
                continue
            oid = "w" + str(obj.id)
            holes[oid] = {"id": oid, "tags": {k: v for k, v in ((t.k, t.v) for t in obj.tags) if k in HOLE_TAG_KEYS}, "coords": coords}
        elif obj.is_relation():
            tags = {t.k: t.v for t in obj.tags}
            # A course inside a club: `golf=course` on a `type=sport` (or
            # site) relation whose members are the course's holes.
            if tags.get("golf") != "course":
                continue
            members = [m.type + str(m.ref) for m in obj.members if m.type == "w"]
            oid = "r" + str(obj.id)
            groups[oid] = {"id": oid, "tags": {k: v for k, v in tags.items() if k.startswith(("name", "golf", "ref"))}, "members": members}

    return {
        "region": region,
        "country": country,
        # The extract's replication time (Geofabrik's file header), so the
        # build can say how fresh the data is.
        "timestamp": timestamp,
        "courses": sorted(courses.values(), key=lambda c: c["id"]),
        "holes": sorted(holes.values(), key=lambda h: h["id"]),
        "groups": sorted(groups.values(), key=lambda g: g["id"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input")
    parser.add_argument("--region", required=True)
    parser.add_argument("--country", required=True, help="ISO 3166-1 alpha-2 of the region")
    parser.add_argument("--out", required=True, help="output .json.gz")
    parser.add_argument("--timestamp", help="the OSM data's time, e.g. from `osmium fileinfo -g header.option.timestamp`")
    args = parser.parse_args(argv)
    raw = extract(args.input, args.region, args.country.upper(), args.timestamp or None)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{args.region}: {len(raw['courses'])} courses, {len(raw['holes'])} holes, {len(raw['groups'])} course relations", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
