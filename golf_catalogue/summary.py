"""Write the run summary (Markdown) from a build's stats and log.

    python -m golf_catalogue.summary out/ --minutes 42 >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n / 1_048_576:.2f} MB" if n >= 1_048_576 else f"{n / 1024:.0f} KB"


def summary(out_dir: Path, minutes: float | None = None) -> str:
    stats = json.loads((out_dir / "stats.json").read_text(encoding="utf-8"))
    log = json.loads((out_dir / "build-log.json").read_text(encoding="utf-8"))
    t = stats["totals"]
    pct = lambda a, b: f"{(a / b):.0%}" if b else "–"  # noqa: E731
    lines = [
        "## Golf catalogue",
        "",
        f"OSM data of {stats['osm_version_date']} · {len(log['regions'])} regions"
        + (f" · {minutes:.0f} min" if minutes is not None else ""),
        "",
        f"**{t['courses']} courses**, {t['with_holes']} with hole data ({pct(t['with_holes'], t['courses'])}), "
        f"{t['complete_18']} complete 18-hole and {t['complete_9']} complete 9-hole layouts.",
        "",
        f"Search index: {_size(out_dir / 'search-index.json.gz')} · course files: "
        f"{sum(1 for _ in (out_dir / 'courses').rglob('*.json'))} · dropped: "
        + (", ".join(f"{v} {k}" for k, v in log["dropped"].items()) or "none")
        + f" · quality notes: {len(log['quality'])}",
        "",
        "| Country | Courses | With holes | Complete 18 | Complete 9 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for country, s in sorted(stats["countries"].items(), key=lambda kv: -kv[1]["courses"]):
        lines.append(f"| {country} | {s['courses']} | {s['with_holes']} ({pct(s['with_holes'], s['courses'])}) | {s['complete_18']} | {s['complete_9']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir")
    parser.add_argument("--minutes", type=float)
    args = parser.parse_args(argv)
    print(summary(Path(args.out_dir), args.minutes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
