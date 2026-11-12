"""The tennis run summary (Markdown), with the countries the brief asks to
see after the first run.

    python -m tennis_catalogue.summary out-tennis/ >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

FOCUS = ("LT", "LV", "EE", "PL", "DE", "SE", "GB", "ES", "FR", "IT", "US", "AU", "ZA")


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n / 1_048_576:.2f} MB" if n >= 1_048_576 else f"{n / 1024:.0f} KB"


def summary(out_dir: Path) -> str:
    stats = json.loads((out_dir / "stats.json").read_text(encoding="utf-8"))
    log = json.loads((out_dir / "build-log.json").read_text(encoding="utf-8"))
    t = stats["totals"]
    lines = [
        "## Tennis venues",
        "",
        f"OSM data of {stats['osm_version_date']} · {len(log['regions'])} regions",
        "",
        f"**{t['venues']} venues**, {t['courts']} courts · search index {_size(out_dir / 'search-index.json.gz')} · "
        f"dropped: " + (", ".join(f"{v} {k}" for k, v in log["dropped"].items()) or "none")
        + f" · kept for later: {log['later'].get('padel', 0)} padel, {log['later'].get('pickleball', 0)} pickleball venues",
        "",
        "| Country | Raw | Pitches | Indoor dropped | Backyard hidden | Venues | Surface | Lit known | Access known | Axis high |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    rows = sorted(stats["countries"].items(), key=lambda kv: (kv[0] not in FOCUS, FOCUS.index(kv[0]) if kv[0] in FOCUS else 0, -kv[1]["venues"]))
    for country, s in rows:
        lines.append(
            f"| {country} | {s['raw_objects']} | {s['pitches_kept']} | {s['indoor_dropped']} ({s['pct_indoor_dropped']}%) | "
            f"{s['backyard_hidden']} | {s['venues']} | {s['pct_with_surface']}% | {s['pct_lit_known']}% | "
            f"{s['pct_access_known']}% | {s['pct_axis_high']}% |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir")
    args = parser.parse_args(argv)
    print(summary(Path(args.out_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
