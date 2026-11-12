"""Between the two osmium passes: the area around every racket-sport pitch,
as one GeoJSON polygon for `osmium extract -p`, so the surroundings pass
(buildings, parks, residential land, roads, water) keeps only what is near a
court.

    python -m tennis_catalogue.boxes racket.osm.pbf --out boxes.geojson

Exit 0 with no file when the region has no such pitch: the workflow then
skips the surroundings pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import osmium
import shapely
from shapely.geometry import box, mapping

from golf_catalogue import geo

from .extract import is_racket

AROUND_M = 250.0


def boxes(path: str | Path) -> shapely.Geometry | None:
    shapes = []
    processor = osmium.FileProcessor(str(path)).with_locations()
    for obj in processor:
        if obj.is_node():
            if not obj.location.valid() or not obj.tags:
                continue
            tags = {t.k: t.v for t in obj.tags}
            if not is_racket(tags):
                continue
            points = [(obj.location.lat, obj.location.lon)]
        elif obj.is_way():
            tags = {t.k: t.v for t in obj.tags}
            if not is_racket(tags):
                continue
            points = [(n.lat, n.lon) for n in obj.nodes if n.location.valid()]
        else:
            continue
        if not points:
            continue
        lat = sum(p[0] for p in points) / len(points)
        pad = geo.metres_to_degrees(AROUND_M, lat)
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        shapes.append(box(min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad))
    if not shapes:
        return None
    return shapely.union_all(shapes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    area = boxes(args.input)
    if area is None:
        print("No racket-sport pitches: no surroundings pass.", file=sys.stderr)
        return 0
    feature = {"type": "Feature", "properties": {}, "geometry": mapping(area)}
    Path(args.out).write_text(json.dumps(feature), encoding="utf-8")
    parts = len(getattr(area, "geoms", [area]))
    print(f"{parts} areas around racket-sport pitches", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
