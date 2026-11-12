"""The safety guard for tennis: never publish a build that lost a large part
of the world's venues (20 % in a country with 20 or more, 5 % worldwide).

    python -m tennis_catalogue.guard out-tennis/stats.json [--previous manifest.json]

Also refuses a search index over `MAX_INDEX_MB` gzipped: the app downloads it
whole, and the brief asks to split it per country before that happens.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_COUNTRY_LOSS = 0.20
MAX_WORLD_LOSS = 0.05
MIN_COUNTRY_VENUES = 20
MAX_INDEX_MB = 8.0


def check(new_stats: dict, previous_counts: dict | None, index_bytes: int | None = None) -> list[str]:
    problems = []
    if index_bytes is not None and index_bytes > MAX_INDEX_MB * 1_048_576:
        problems.append(f"search index is {index_bytes / 1_048_576:.1f} MB gzipped, over {MAX_INDEX_MB} MB: split it per country first")
    if not previous_counts:
        return problems
    old_world = previous_counts.get("totals", {}).get("venues", 0)
    new_world = new_stats["totals"]["venues"]
    if old_world and new_world < old_world * (1 - MAX_WORLD_LOSS):
        problems.append(f"world: {old_world} → {new_world} venues ({(1 - new_world / old_world):.1%} fewer)")
    for country, old in sorted(previous_counts.get("countries", {}).items()):
        before = old.get("venues", 0)
        if before < MIN_COUNTRY_VENUES:
            continue
        after = new_stats["countries"].get(country, {}).get("venues", 0)
        if after < before * (1 - MAX_COUNTRY_LOSS):
            problems.append(f"{country}: {before} → {after} venues ({(1 - after / before):.1%} fewer)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refuse a tennis build that lost too many venues.")
    parser.add_argument("stats")
    parser.add_argument("--previous")
    args = parser.parse_args(argv)
    stats_path = Path(args.stats)
    new_stats = json.loads(stats_path.read_text(encoding="utf-8"))
    index = stats_path.parent / "search-index.json.gz"
    previous = None
    if args.previous and Path(args.previous).exists():
        previous = json.loads(Path(args.previous).read_text(encoding="utf-8")).get("counts")
    problems = check(new_stats, previous, index.stat().st_size if index.exists() else None)
    for problem in problems:
        print(f"::error::Tennis catalogue guard: {problem}", file=sys.stderr)
    if problems:
        print("Not publishing tennis; the last good build stays live.", file=sys.stderr)
        return 1
    print("Guard passed." if previous else "No previous tennis build: guard skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
