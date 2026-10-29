"""The safety guard: never publish a build that lost a large part of the world.

    python -m golf_catalogue.guard out/stats.json [--previous manifest.json]

Compares the new build's counts with the live manifest's. Fails (exit 1) when
a country lost more than 20 % of its courses or the world more than 5 %,
so a broken extract or a bad OSM day keeps the last good build live.

Countries with fewer than `MIN_COUNTRY_COURSES` courses before are checked
against the world total only: a country going from 4 courses to 3 is a 25 %
drop and an ordinary month in OpenStreetMap.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_COUNTRY_LOSS = 0.20
MAX_WORLD_LOSS = 0.05
MIN_COUNTRY_COURSES = 20


def check(new_stats: dict, previous_counts: dict | None) -> list[str]:
    """Reasons not to publish; empty when the build is fine."""
    if not previous_counts:
        return []
    problems = []
    old_world = previous_counts.get("totals", {}).get("courses", 0)
    new_world = new_stats["totals"]["courses"]
    if old_world and new_world < old_world * (1 - MAX_WORLD_LOSS):
        problems.append(f"world: {old_world} → {new_world} courses ({(1 - new_world / old_world):.1%} fewer)")
    new_countries = new_stats["countries"]
    for country, old in sorted(previous_counts.get("countries", {}).items()):
        before = old.get("courses", 0)
        if before < MIN_COUNTRY_COURSES:
            continue
        after = new_countries.get(country, {}).get("courses", 0)
        if after < before * (1 - MAX_COUNTRY_LOSS):
            problems.append(f"{country}: {before} → {after} courses ({(1 - after / before):.1%} fewer)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refuse a build that lost too many courses.")
    parser.add_argument("stats")
    parser.add_argument("--previous", help="the live manifest.json; missing on the first run")
    args = parser.parse_args(argv)
    new_stats = json.loads(Path(args.stats).read_text(encoding="utf-8"))
    previous = None
    if args.previous and Path(args.previous).exists():
        previous = json.loads(Path(args.previous).read_text(encoding="utf-8")).get("counts")
    problems = check(new_stats, previous)
    for problem in problems:
        print(f"::error::Golf catalogue guard: {problem}", file=sys.stderr)
    if problems:
        print("Not publishing; the last good build stays live.", file=sys.stderr)
        return 1
    print("Guard passed." if previous else "No previous build: guard skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
