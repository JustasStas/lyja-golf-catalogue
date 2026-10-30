"""Print the GitHub Actions matrix for the region extracts.

    python -m golf_catalogue.plan [--only REGEX] > matrix.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

REGIONS = Path(__file__).resolve().parent.parent / "regions.yaml"
GEOFABRIK = "https://download.geofabrik.de/"


def regions(only: str | None = None) -> list[dict]:
    entries = yaml.safe_load(REGIONS.read_text(encoding="utf-8"))["regions"]
    out = []
    for entry in entries:
        path = entry["path"]
        country = entry["country"]
        # A bare NO in YAML is the boolean false (Norway came out as
        # "FALSE"): every code must be a two-letter string.
        if not (isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country)):
            raise SystemExit(f"regions.yaml: {path}: country {country!r} is not a quoted ISO code")
        if only and not re.search(only, path):
            continue
        out.append({
            "id": path.replace("/", "-"),
            "url": f"{GEOFABRIK}{path}-latest.osm.pbf",
            "country": country,
        })
    ids = [r["id"] for r in out]
    if len(ids) != len(set(ids)):
        raise SystemExit("regions.yaml: duplicate region")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="regex on the Geofabrik path, e.g. 'lithuania|latvia'")
    args = parser.parse_args(argv)
    print(json.dumps({"include": regions(args.only)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
