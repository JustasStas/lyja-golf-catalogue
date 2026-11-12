"""Stage 1 for tennis, per region: read the filtered OSM files and keep only
what the venue catalogue needs.

Input is two files from the region's extract (any format pyosmium reads; the
tests pass one small `.osm` file as both):

- the racket-sport pass,
  `osmium tags-filter region.osm.pbf nwr/sport=tennis,padel,pickleball
  nwr/leisure=pitch,practice_pitch,sports_centre,sports_hall,stadium nwr/club=sport`
- the surroundings pass (`boxes` then `osmium extract`): buildings,
  residential land, parks, recreation grounds, roads and water near those
  pitches only, so a country's buildings never reach this step.

Output is one gzipped JSON document per region:

    {"region": "lithuania", "country": "LT", "timestamp": "...",
     "pitches":    [{"id": "w1", "tags": {...}, "wkt": "POLYGON(...)", "in": ["w2"]} | {..., "point": [lat, lon]}],
     "containers": [{"id": "w2", "kind": "sports_centre", "name": "...", "tags": {...}, "area_ha": 1.2}],
     "points":     [{"id": "n3", "tags": {...}, "point": [lat, lon]}],
     "barriers":   [{"id": "w4", "kind": "road", "name": "...", "coords": [[lat, lon], ...]}]}

Nothing here decides what a venue is: exclusions, grouping, names, courts,
surfaces and axes happen in `build`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

import osmium
import shapely
import shapely.wkb
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from golf_catalogue import geo

RACKET_SPORTS = ("tennis", "padel", "pickleball")
PITCH_TAG_PREFIXES = (
    "sport", "leisure", "surface", "lit", "access", "indoor", "covered", "name", "courts", "pitch:count", "count",
    "clay", "surface:colour", "building", "disused", "abandoned", "operator", "website", "contact:website", "club",
)
CONTAINER_TAG_PREFIXES = ("name", "leisure", "club", "landuse", "building", "sport", "operator", "website", "contact:website", "access")
ROADS = {"primary", "secondary", "tertiary", "residential"}
# Roads and water kept only this close to a racket-sport pitch: the build
# needs them for the 40 m clustering and street names within 80 m.
BARRIER_NEAR_M = 100.0
WATERWAYS = {"river", "canal", "stream"}


def sports(tags: dict[str, str]) -> set[str]:
    """The sports a feature is for: `sport=tennis;basketball` is both."""
    return {s.strip() for s in tags.get("sport", "").split(";") if s.strip()}


def is_racket(tags: dict[str, str]) -> bool:
    return bool(sports(tags) & set(RACKET_SPORTS))


def container_kind(tags: dict[str, str]) -> str | None:
    """What an area can be to a pitch inside it, or None."""
    leisure = tags.get("leisure")
    if leisure in ("sports_hall",):
        return "sports_hall"
    if "building" in tags and tags.get("building") != "no":
        return "building"
    if leisure in ("sports_centre", "stadium"):
        return leisure
    if tags.get("club") == "sport":
        return "club"
    if tags.get("landuse") == "recreation_ground":
        return "recreation_ground"
    if leisure == "park":
        return "park"
    if tags.get("landuse") == "residential":
        return "residential"
    return None


def _keep(tags: dict[str, str], prefixes) -> dict[str, str]:
    return {k: v for k, v in tags.items() if k.startswith(prefixes)}


def _read(path: Path, out: dict) -> None:
    wkb = osmium.geom.WKBFactory()
    processor = osmium.FileProcessor(str(path)).with_locations().with_areas()
    for obj in processor:
        if obj.is_area():
            tags = {t.k: t.v for t in obj.tags}
            oid = ("w" if obj.from_way() else "r") + str(obj.orig_id())
            is_pitch = tags.get("leisure") in ("pitch", "practice_pitch")
            kind = None if is_pitch else container_kind(tags)
            if not is_pitch and kind is None:
                continue
            try:
                geom = shapely.wkb.loads(wkb.create_multipolygon(obj), hex=True)
            except Exception:  # broken multipolygon in OSM: skipped
                continue
            wkt = shapely.to_wkt(geom, rounding_precision=7)
            if is_pitch:
                out["pitches"][oid] = {"id": oid, "tags": _keep(tags, PITCH_TAG_PREFIXES), "wkt": wkt}
            else:
                out["containers"][oid] = {
                    "id": oid, "kind": kind, "name": tags.get("name"), "tags": _keep(tags, CONTAINER_TAG_PREFIXES), "wkt": wkt,
                }
        elif obj.is_node():
            tags = {t.k: t.v for t in obj.tags}
            if not tags or not obj.location.valid():
                continue
            point = [round(obj.location.lat, 7), round(obj.location.lon, 7)]
            oid = "n" + str(obj.id)
            if tags.get("leisure") in ("pitch", "practice_pitch"):
                out["pitches"][oid] = {"id": oid, "tags": _keep(tags, PITCH_TAG_PREFIXES), "point": point}
            elif "name" in tags and (is_racket(tags) or tags.get("club") == "sport" or tags.get("leisure") == "sports_centre"):
                out["points"][oid] = {"id": oid, "tags": _keep(tags, CONTAINER_TAG_PREFIXES), "point": point}
        elif obj.is_way():
            highway = obj.tags.get("highway")
            waterway = obj.tags.get("waterway")
            kind = "road" if highway in ROADS else ("water" if waterway in WATERWAYS else None)
            if kind is None:
                continue
            coords = [[round(n.lat, 7), round(n.lon, 7)] for n in obj.nodes if n.location.valid()]
            if len(coords) < 2:
                continue
            oid = "w" + str(obj.id)
            out["barriers"][oid] = {"id": oid, "kind": kind, "name": obj.tags.get("name"), "coords": coords}


def extract(paths: list[str | Path], region: str, country: str, timestamp: str | None = None) -> dict:
    out: dict[str, dict] = {"pitches": {}, "containers": {}, "points": {}, "barriers": {}}
    for path in paths:
        if path and Path(path).exists():
            _read(Path(path), out)
    return reduce(out, region, country, timestamp)


def reduce(out: dict, region: str, country: str, timestamp: str | None) -> dict:
    """Keeps only what the build needs, so a country's buildings and
    residential land never leave the region's job:

    - racket-sport pitches only (other sports' pitches are not venues, and a
      pitch around tennis pitches is tennis itself when it is double mapped);
    - each pitch's containers worked out here (`in`: the ids of every area
      covering its centre), and only those containers kept, without their
      geometry but with their area for picking the smallest;
    - roads and water within `BARRIER_NEAR_M` of a pitch.
    """
    pitches = {oid: p for oid, p in out["pitches"].items() if is_racket(p["tags"])}
    containers = {oid: c for oid, c in out["containers"].items() if oid not in pitches}
    container_ids = list(containers)
    geoms = [shapely.from_wkt(containers[i]["wkt"]) for i in container_ids]
    tree = STRtree(geoms) if geoms else None
    used: set[str] = set()
    pitch_shapes = []
    for p in pitches.values():
        geom = shapely.from_wkt(p["wkt"]) if "wkt" in p else Point(p["point"][1], p["point"][0])
        pitch_shapes.append(geom)
        centre = geom.representative_point() if geom.geom_type != "Point" else geom
        inside = []
        if tree is not None:
            for i in tree.query(centre):
                if geoms[int(i)].covers(centre):
                    inside.append(container_ids[int(i)])
        p["in"] = sorted(inside)
        used.update(inside)
    kept_containers = []
    for oid in sorted(used):
        c = containers[oid]
        kept_containers.append({
            "id": oid, "kind": c["kind"], "name": c["name"], "tags": c["tags"],
            "area_ha": round(geo.geometry_area_ha(geoms[container_ids.index(oid)]), 4),
        })
    barriers = []
    if pitch_shapes and out["barriers"]:
        pitch_tree = STRtree(pitch_shapes)
        for b in out["barriers"].values():
            line = LineString([(lon, lat) for lat, lon in b["coords"]])
            pad = geo.metres_to_degrees(BARRIER_NEAR_M, b["coords"][0][0])
            if len(pitch_tree.query(line.buffer(pad))):
                barriers.append(b)
    return {
        "region": region,
        "country": country,
        "timestamp": timestamp,
        "pitches": sorted(pitches.values(), key=lambda v: v["id"]),
        "containers": kept_containers,
        "points": sorted(out["points"].values(), key=lambda v: v["id"]),
        "barriers": sorted(barriers, key=lambda v: v["id"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inputs", nargs="+", help="the racket pass, then the surroundings pass")
    parser.add_argument("--region", required=True)
    parser.add_argument("--country", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timestamp")
    args = parser.parse_args(argv)
    raw = extract(args.inputs, args.region, args.country.upper(), args.timestamp or None)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, separators=(",", ":"))
    print(
        f"{args.region}: {len(raw['pitches'])} pitches, {len(raw['containers'])} containers, "
        f"{len(raw['points'])} named points, {len(raw['barriers'])} roads and waterways",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
