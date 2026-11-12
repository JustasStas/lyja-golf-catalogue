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
     "pitches":    [{"id": "w1", "tags": {...}, "wkt": "POLYGON(...)"} | {..., "point": [lat, lon]}],
     "containers": [{"id": "w2", "kind": "sports_centre", "name": "...", "tags": {...}, "wkt": "..."}],
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
import shapely.wkb

RACKET_SPORTS = ("tennis", "padel", "pickleball")
PITCH_TAG_PREFIXES = (
    "sport", "leisure", "surface", "lit", "access", "indoor", "covered", "name", "courts", "pitch:count", "count",
    "clay", "surface:colour", "building", "disused", "abandoned", "operator", "website", "contact:website", "club",
)
CONTAINER_TAG_PREFIXES = ("name", "leisure", "club", "landuse", "building", "sport", "operator", "website", "contact:website", "access")
ROADS = {"primary", "secondary", "tertiary", "residential"}
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
    # A pitch that is also a container (a sports centre mapped as one area
    # with leisure=pitch) stays a pitch only.
    for oid in list(out["containers"]):
        if oid in out["pitches"]:
            del out["containers"][oid]
    return {
        "region": region,
        "country": country,
        "timestamp": timestamp,
        **{key: sorted(values.values(), key=lambda v: v["id"]) for key, values in out.items()},
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
