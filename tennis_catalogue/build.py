"""Stage 2 for tennis, once: merge every region's raw extract into the venue
catalogue (`tennis/v1/`, the app's `TennisVenue` and `TennisIndexEntry`).

    python -m tennis_catalogue.build raw-tennis/ out-tennis/

writes

    out/search-index.json.gz      every outdoor venue, compact (backyard courts left out)
    out/venues/{CC}/{id}.json     one venue
    out/countries/{CC}.json.gz    every venue of a country
    out/stats.json                per-country coverage
    out/build-log.json            counts (not published)
    out/later/{padel,pickleball}.json.gz   the same grouping for those sports, kept for later, not published

The rules, in the brief's order (Part A, Extraction):
1. Courts are `leisure=pitch` with `sport=tennis` (one of a `;` list).
2. Dropped as indoor: `indoor=yes`, `covered=yes`, a pitch inside a
   building or a `leisure=sports_hall`. `covered=seasonal` is kept and
   flagged. Dropped as disused: any `disused:*` or `abandoned:*` key.
   `leisure=practice_pitch` (walls) is left out and counted.
3. Double mapping: a pitch that holds smaller tennis pitches is dropped.
4. Venues: the smallest sports centre, `club=sport`, or named recreation
   ground or park around a pitch; otherwise pitches within 40 m of each
   other (DBSCAN, eps 40 m, min 1) that no main road, residential street or
   waterway separates.
5. A single unnamed court, private or inside residential land, with no club
   or sports centre around it, is a backyard court: counted, not indexed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import shapely
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from golf_catalogue import countries, geo

from .extract import sports

CLUSTER_EPS_M = 40.0
NAMED_POINT_M = 100.0
STREET_NAME_M = 80.0
SINGLE_COURT_PERIMETER_M = 115.0
COURT_AREA_M2 = 650.0
AXIS_DISAGREE_DEG = 30.0
ATTRIBUTION = "© OpenStreetMap contributors, ODbL"

SURFACES = ("hard", "red_clay", "green_clay", "artificial_grass", "grass", "porous_asphalt")
INDEX_SURFACE = {
    "hard": "h", "red_clay": "rc", "green_clay": "gc", "artificial_grass": "ag", "grass": "g", "porous_asphalt": "pa",
}
VENUE_CONTAINERS = ("sports_centre", "club", "stadium")
NAMED_CONTAINERS = ("recreation_ground", "park")
INDOOR_CONTAINERS = ("building", "sports_hall")


def surface_of(tags: dict[str, str]) -> str:
    """The brief's mapping of OSM `surface` to the catalogue's surfaces."""
    surface = tags.get("surface", "").strip().lower()
    if surface == "clay":
        colour = (tags.get("clay:type", "") + " " + tags.get("surface:colour", "")).lower()
        return "green_clay" if ("green" in colour or "har-tru" in colour or "hartru" in colour) else "red_clay"
    if surface in ("hard", "acrylic", "asphalt", "concrete"):
        return "hard"
    if surface == "grass":
        return "grass"
    if surface == "artificial_turf":
        return "artificial_grass"
    return "unknown"


def _local(geom, lat0: float):
    """A lon/lat geometry in metres around `lat0` (equirectangular; a pitch
    or a venue is far too small for the difference to matter)."""
    k = math.cos(math.radians(lat0))
    return shapely.transform(geom, lambda xy: xy * [111_320.0 * k, 110_574.0])


def _bearing(dx: float, dy: float) -> float:
    """Axial bearing of a vector in local metres, 0 ≤ b < 180."""
    return math.degrees(math.atan2(dx, dy)) % 180.0


def _axial_diff(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


@dataclass
class Pitch:
    id: str
    tags: dict[str, str]
    geom: object  # shapely, lon/lat
    lat: float
    lon: float
    country: str
    courts: int = 1
    perimeter_m: float = 0.0
    area_m2: float = 0.0
    axis: float | None = None
    axis_high: bool = False
    seasonal: bool = False

    @property
    def name(self) -> str | None:
        return self.tags.get("name")


@dataclass
class Venue:
    id: str
    country: str
    pitches: list[Pitch] = field(default_factory=list)
    container: dict | None = None
    name: str | None = None
    name_generated: bool = False
    name_base: str | None = None


def court_count(tags: dict[str, str], perimeter_m: float, area_m2: float, is_point: bool) -> int:
    """Rule 5: the tag, else one court under 115 m round, else area ÷ 650 m²."""
    for key in ("courts", "pitch:count", "count"):
        value = tags.get(key, "").strip()
        if value.isdigit() and int(value) > 0:
            return int(value)
    if is_point or perimeter_m < SINGLE_COURT_PERIMETER_M:
        return 1
    return max(1, round(area_m2 / COURT_AREA_M2))


def pitch_axis(local_geom, courts: int, perimeter_m: float) -> tuple[float | None, bool]:
    """Rule 7: the long-axis bearing from the minimum rotated rectangle, and
    whether it is a single court's own (high confidence)."""
    if local_geom is None or local_geom.geom_type not in ("Polygon", "MultiPolygon"):
        return None, False
    rect = local_geom.minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        return None, False
    xs, ys = rect.exterior.coords.xy
    edges = [(xs[i + 1] - xs[i], ys[i + 1] - ys[i]) for i in range(2)]
    lengths = [math.hypot(dx, dy) for dx, dy in edges]
    if min(lengths) <= 0.5:
        return None, False
    long_i = 0 if lengths[0] >= lengths[1] else 1
    short_i = 1 - long_i
    ratio = lengths[long_i] / lengths[short_i]
    if courts <= 1 and perimeter_m < SINGLE_COURT_PERIMETER_M:
        # A single court is about 2:1 (36 × 18 m with its run-off).
        return _bearing(*edges[long_i]), 1.6 <= ratio <= 2.6
    # Several courts in one pitch: side by side, the courts run along the
    # rectangle's short side when it is about one court long.
    side_by_side = 28.0 <= lengths[short_i] <= 45.0 and lengths[long_i] > 45.0
    if side_by_side:
        return _bearing(*edges[short_i]), False
    return _bearing(*edges[long_i]), False


def venue_axis(pitches: list[Pitch]) -> tuple[float | None, str]:
    """The court-weighted circular mean of the pitches' axes (on doubled
    angles: an axis has no direction), `none` when they disagree by more
    than 30°."""
    known = [p for p in pitches if p.axis is not None]
    if not known:
        return None, "none"
    c = sum(p.courts * math.cos(math.radians(2 * p.axis)) for p in known)
    s = sum(p.courts * math.sin(math.radians(2 * p.axis)) for p in known)
    if abs(c) < 1e-9 and abs(s) < 1e-9:
        return None, "none"
    mean = (math.degrees(math.atan2(s, c)) / 2.0) % 180.0
    if any(_axial_diff(p.axis, mean) > AXIS_DISAGREE_DEG for p in known):
        return None, "none"
    confidence = "high" if all(p.axis_high for p in known) and len(known) == len(pitches) else "low"
    return round(mean, 1), confidence


def lit_of(pitches: list[Pitch]) -> str:
    values = [p.tags.get("lit") for p in pitches if p.tags.get("lit") in ("yes", "no")]
    if not values:
        return "unknown"
    if all(v == "yes" for v in values) and len(values) == len(pitches):
        return "yes"
    if any(v == "yes" for v in values):
        return "partial"
    return "no"


def access_of(pitches: list[Pitch], container: dict | None) -> str:
    def norm(value: str | None) -> str | None:
        if value in ("yes", "public", "permissive"):
            return "public"
        if value in ("customers", "members", "private", "permit"):
            return "customers"
        return None
    votes = Counter(v for v in (norm(p.tags.get("access")) for p in pitches) if v)
    if not votes and container is not None:
        own = norm(container.get("tags", {}).get("access"))
        if own:
            votes[own] += 1
    return votes.most_common(1)[0][0] if votes else "unknown"


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def cluster(pitches: list[Pitch], barriers: list[LineString], barrier_tree: STRtree | None) -> list[list[Pitch]]:
    """DBSCAN with eps 40 m and min 1 (so: connected components of the
    within-40 m graph), never across a barrier."""
    if not pitches:
        return []
    points = [Point(p.lon, p.lat) for p in pitches]
    tree = STRtree(points)
    uf = _UnionFind(len(pitches))
    for i, p in enumerate(pitches):
        pad = geo.metres_to_degrees(CLUSTER_EPS_M + 60, p.lat)
        for j in tree.query(points[i].buffer(pad)):
            j = int(j)
            if j <= i:
                continue
            q = pitches[j]
            # Edge to edge for polygons: two long pitches can be 40 m apart
            # though their centres are 70 m apart.
            gap = _gap_m(p, q)
            if gap > CLUSTER_EPS_M:
                continue
            link = LineString([(p.lon, p.lat), (q.lon, q.lat)])
            if barrier_tree is not None and any(
                link.crosses(barriers[int(k)]) for k in barrier_tree.query(link)
            ):
                continue
            uf.union(i, j)
    groups: dict[int, list[Pitch]] = defaultdict(list)
    for i, p in enumerate(pitches):
        groups[uf.find(i)].append(p)
    return list(groups.values())


def _gap_m(a: Pitch, b: Pitch) -> float:
    lat0 = (a.lat + b.lat) / 2
    return _local(a.geom, lat0).distance(_local(b.geom, lat0))


def _load(raw_dir: Path) -> list[dict]:
    raws = []
    for path in sorted(raw_dir.glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raws.append(json.load(f))
    return raws


def _geom(item: dict):
    if "wkt" in item:
        return shapely.from_wkt(item["wkt"])
    lat, lon = item["point"]
    return Point(lon, lat)


def build_sport(raws: list[dict], sport: str, osm_date: str, out_dir: Path | None, publish: bool) -> dict:
    """Venues for one sport. `publish` writes the app's files under
    `out_dir`; otherwise only the grouped venues come back (padel and
    pickleball, kept for later)."""
    stats: dict[str, Counter] = defaultdict(Counter)
    dropped = Counter()

    # Everything from every region, de-duplicated by OSM id (a feature on a
    # border is in both extracts).
    pitches_raw: dict[str, tuple[dict, str]] = {}
    containers: dict[str, dict] = {}
    points: dict[str, dict] = {}
    barriers_raw: dict[str, dict] = {}
    for raw in raws:
        for p in raw["pitches"]:
            pitches_raw.setdefault(p["id"], (p, raw["country"]))
        for c in raw["containers"]:
            containers.setdefault(c["id"], c)
        for n in raw["points"]:
            points.setdefault(n["id"], n)
        for b in raw["barriers"]:
            barriers_raw.setdefault(b["id"], b)

    container_list = list(containers.values())
    container_geoms = [shapely.from_wkt(c["wkt"]) for c in container_list]
    container_tree = STRtree(container_geoms) if container_geoms else None
    barriers = [LineString([(lon, lat) for lat, lon in b["coords"]]) for b in barriers_raw.values()]
    barrier_names = [b.get("name") for b in barriers_raw.values()]
    barrier_kinds = [b["kind"] for b in barriers_raw.values()]
    barrier_tree = STRtree(barriers) if barriers else None

    def around(point: Point) -> list[tuple[dict, object]]:
        if container_tree is None:
            return []
        return [
            (container_list[int(i)], container_geoms[int(i)])
            for i in container_tree.query(point)
            if container_geoms[int(i)].covers(point)
        ]

    # 1, 2: the sport's outdoor pitches.
    kept: list[Pitch] = []
    for pid, (item, region_country) in sorted(pitches_raw.items()):
        tags = item["tags"]
        if sport not in sports(tags):
            continue
        geom = _geom(item)
        centre = geom.representative_point() if geom.geom_type != "Point" else geom
        lat, lon = centre.y, centre.x
        country = countries.resolve(region_country, tags, lat, lon)
        stats[country]["raw_objects"] += 1
        if tags.get("leisure") == "practice_pitch":
            stats[country]["practice_walls"] += 1
            dropped["practice wall"] += 1
            continue
        if any(k.startswith(("disused", "abandoned")) for k in tags):
            dropped["disused"] += 1
            stats[country]["disused_dropped"] += 1
            continue
        covered = tags.get("covered", "")
        inside = around(centre)
        if (
            tags.get("indoor") == "yes"
            or covered == "yes"
            or ("building" in tags and tags.get("building") != "no")
            or any(c["kind"] in INDOOR_CONTAINERS for c, _ in inside)
        ):
            dropped["indoor"] += 1
            stats[country]["indoor_dropped"] += 1
            continue
        pitch = Pitch(id=pid, tags=tags, geom=geom, lat=lat, lon=lon, country=country, seasonal=covered == "seasonal")
        if geom.geom_type != "Point":
            local = _local(geom, lat)
            pitch.perimeter_m = local.length
            pitch.area_m2 = local.area
            pitch.courts = court_count(tags, pitch.perimeter_m, pitch.area_m2, False)
            pitch.axis, pitch.axis_high = pitch_axis(local, pitch.courts, pitch.perimeter_m)
        else:
            pitch.courts = court_count(tags, 0, 0, True)
        kept.append(pitch)

    # 3: double mapping, a pitch holding smaller pitches of the sport.
    polygons = [p for p in kept if p.geom.geom_type != "Point"]
    holders = set()
    if polygons:
        tree = STRtree([p.geom for p in polygons])
        for p in polygons:
            for i in tree.query(p.geom):
                q = polygons[int(i)]
                if q is p:
                    continue
                if p.area_m2 > q.area_m2 * 1.5 and p.geom.covers(Point(q.lon, q.lat)):
                    holders.add(p.id)
                    break
    for p in kept:
        if p.id in holders:
            dropped["double mapped"] += 1
            stats[p.country]["double_mapped_dropped"] += 1
    kept = [p for p in kept if p.id not in holders]

    # 4: venues, by the feature around them, else by clustering.
    by_container: dict[str, Venue] = {}
    loose: list[Pitch] = []
    for p in kept:
        inside = [
            (c, g) for c, g in around(Point(p.lon, p.lat))
            if c["kind"] in VENUE_CONTAINERS or (c["kind"] in NAMED_CONTAINERS and c.get("name"))
        ]
        if inside:
            c, _ = min(inside, key=lambda cg: geo.geometry_area_ha(cg[1]))
            venue = by_container.setdefault(c["id"], Venue(id=c["id"], country=p.country, container=c))
            venue.pitches.append(p)
        else:
            loose.append(p)
    venues = list(by_container.values())
    for group in cluster(loose, barriers, barrier_tree):
        group.sort(key=lambda p: p.id)
        venues.append(Venue(id=group[0].id, country=group[0].country, pitches=group))

    # Names.
    point_list = list(points.values())
    point_geoms = [Point(n["point"][1], n["point"][0]) for n in point_list]
    point_tree = STRtree(point_geoms) if point_geoms else None
    for venue in venues:
        _name(venue, point_list, point_geoms, point_tree, barriers, barrier_names, barrier_kinds, barrier_tree, around)

    if not publish or out_dir is None:
        return {"venues": [_venue_json(v, osm_date) for v in venues]}

    out_dir.mkdir(parents=True, exist_ok=True)
    index: list[dict] = []
    by_country: dict[str, list[dict]] = defaultdict(list)
    for venue in sorted(venues, key=lambda v: v.id):
        doc = _venue_json(venue, osm_date)
        s = stats[venue.country]
        s["pitches_kept"] += len(venue.pitches)
        s["courts"] += doc["courts_est"]
        if _is_backyard(venue, around):
            dropped["backyard"] += 1
            s["backyard_hidden"] += 1
            continue
        s["venues"] += 1
        s["with_surface"] += 1 if doc["surfaces"]["unknown"] < doc["courts_est"] else 0
        s["lit_known"] += 1 if doc["lit"] != "unknown" else 0
        s["access_known"] += 1 if doc["access"] != "unknown" else 0
        s["axis_high"] += 1 if doc["axis_confidence"] == "high" else 0
        s["seasonal_cover"] += 1 if doc["covered_share"] > 0 else 0
        index.append(_index_entry(doc))
        by_country[venue.country].append(doc)
        path = out_dir / "venues" / venue.country / f"{venue.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    with gzip.open(out_dir / "search-index.json.gz", "wt", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "countries").mkdir(exist_ok=True)
    for country, items in by_country.items():
        with gzip.open(out_dir / "countries" / f"{country}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, separators=(",", ":"))

    countries_out = {c: _country_stats(stats[c]) for c in sorted(stats)}
    totals = Counter()
    for s in stats.values():
        totals.update(s)
    stats_out = {"osm_version_date": osm_date, "totals": _country_stats(totals), "countries": countries_out}
    (out_dir / "stats.json").write_text(json.dumps(stats_out, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"stats": stats_out, "dropped": dict(dropped)}


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _country_stats(s: Counter) -> dict:
    raw = s["raw_objects"]
    venues = s["venues"]
    return {
        "raw_objects": raw,
        "pitches_kept": s["pitches_kept"],
        "courts": s["courts"],
        "indoor_dropped": s["indoor_dropped"],
        "disused_dropped": s["disused_dropped"],
        "practice_walls": s["practice_walls"],
        "double_mapped_dropped": s["double_mapped_dropped"],
        "backyard_hidden": s["backyard_hidden"],
        "venues": venues,
        "pct_with_surface": _pct(s["with_surface"], venues),
        "pct_lit_known": _pct(s["lit_known"], venues),
        "pct_access_known": _pct(s["access_known"], venues),
        "pct_axis_high": _pct(s["axis_high"], venues),
        "pct_indoor_dropped": _pct(s["indoor_dropped"], raw),
        "seasonal_cover": s["seasonal_cover"],
    }


def _is_backyard(venue: Venue, around) -> bool:
    """Rule 5: one unnamed court, private or in residential land, and no club
    or sports centre around it."""
    if venue.container is not None and venue.container["kind"] in VENUE_CONTAINERS:
        return False
    if len(venue.pitches) != 1 or venue.pitches[0].courts != 1:
        return False
    pitch = venue.pitches[0]
    if pitch.name or (venue.name and not venue.name_generated):
        return False
    residential = any(c["kind"] == "residential" for c, _ in around(Point(pitch.lon, pitch.lat)))
    return pitch.tags.get("access") == "private" or residential


def _name(venue, point_list, point_geoms, point_tree, barriers, barrier_names, barrier_kinds, barrier_tree, around) -> None:
    """Rule 4 of the names: the feature's name, the commonest pitch name, a
    named tennis point or club within 100 m, else generated from the park or
    street."""
    if venue.container is not None and venue.container.get("name"):
        venue.name = venue.container["name"]
        return
    names = Counter(p.name for p in venue.pitches if p.name)
    if names:
        venue.name = names.most_common(1)[0][0]
        return
    lat = sum(p.lat for p in venue.pitches) / len(venue.pitches)
    lon = sum(p.lon for p in venue.pitches) / len(venue.pitches)
    centre = Point(lon, lat)
    if point_tree is not None:
        pad = geo.metres_to_degrees(NAMED_POINT_M, lat)
        near = [
            (geo.haversine_m(lat, lon, point_list[int(i)]["point"][0], point_list[int(i)]["point"][1]), point_list[int(i)])
            for i in point_tree.query(centre.buffer(pad))
        ]
        near = [(d, n) for d, n in near if d <= NAMED_POINT_M and n["tags"].get("name")]
        if near:
            venue.name = min(near, key=lambda dn: dn[0])[1]["tags"]["name"]
            return
    base = None
    parks = [c for c, _ in around(centre) if c["kind"] in NAMED_CONTAINERS and c.get("name")]
    if parks:
        base = parks[0]["name"]
    elif barrier_tree is not None:
        pad = geo.metres_to_degrees(STREET_NAME_M, lat)
        streets = [
            (barriers[int(i)].distance(centre), barrier_names[int(i)])
            for i in barrier_tree.query(centre.buffer(pad))
            if barrier_kinds[int(i)] == "road" and barrier_names[int(i)]
        ]
        if streets:
            base = min(streets)[1]
    venue.name_generated = True
    venue.name_base = base
    venue.name = f"Tennis courts, {base}" if base else "Tennis courts"


def _venue_json(venue: Venue, osm_date: str) -> dict:
    courts = sum(p.courts for p in venue.pitches)
    surfaces = {s: 0 for s in SURFACES}
    surfaces["unknown"] = 0
    for p in venue.pitches:
        surfaces[surface_of(p.tags)] += p.courts
    lat = sum(p.lat * p.courts for p in venue.pitches) / courts
    lon = sum(p.lon * p.courts for p in venue.pitches) / courts
    axis, confidence = venue_axis(venue.pitches)
    seasonal = sum(p.courts for p in venue.pitches if p.seasonal)
    container_tags = (venue.container or {}).get("tags", {})
    pitch_tags = [p.tags for p in venue.pitches]

    def first(*keys: str) -> str | None:
        for tags in [container_tags, *pitch_tags]:
            for key in keys:
                if tags.get(key):
                    return tags[key]
        return None

    doc = {
        "id": venue.id,
        "name": venue.name,
        "name_generated": venue.name_generated,
        "country": venue.country,
        "centroid": [round(lat, 6), round(lon, 6)],
        "courts_est": courts,
        "surfaces": surfaces,
        "lit": lit_of(venue.pitches),
        "covered_share": round(seasonal / courts, 2) if courts else 0.0,
        "access": access_of(venue.pitches, venue.container),
        "axis_deg": axis,
        "axis_confidence": confidence,
        "website": first("website", "contact:website"),
        "operator": first("operator"),
        "osm_version_date": osm_date,
        "attribution": ATTRIBUTION,
    }
    if venue.name_generated:
        doc["name_base"] = venue.name_base
    return doc


def _index_entry(doc: dict) -> dict:
    known = {k: v for k, v in doc["surfaces"].items() if k != "unknown" and v > 0}
    main = max(known, key=lambda k: (known[k], k)) if known else None
    if main is not None and known[main] < doc["surfaces"]["unknown"]:
        main = None
    lit = {"yes": 1, "partial": 1, "no": 0}.get(doc["lit"])
    entry = {
        "id": doc["id"],
        "n": doc["name"],
        "c": doc["country"],
        "la": doc["centroid"][0],
        "lo": doc["centroid"][1],
        "k": doc["courts_est"],
        "s": INDEX_SURFACE.get(main, "u") if main else "u",
        "l": lit,
        "a": {"public": "p", "customers": "c"}.get(doc["access"], "u"),
    }
    if doc["name_generated"]:
        # The app writes generated names in the reader's language from the
        # park or street: "Teniso kortai, Bernardinų sodas".
        entry["g"] = 1
        if doc.get("name_base"):
            entry["nb"] = doc["name_base"]
    return entry


def build(raw_dir: Path, out_dir: Path, osm_date: str) -> dict:
    raws = _load(raw_dir)
    stamps = sorted(r["timestamp"][:10] for r in raws if r.get("timestamp"))
    if stamps:
        osm_date = stamps[0]
    result = build_sport(raws, "tennis", osm_date, out_dir, publish=True)
    later = out_dir / "later"
    later.mkdir(parents=True, exist_ok=True)
    kept_later = {}
    for sport in ("padel", "pickleball"):
        venues = build_sport(raws, sport, osm_date, None, publish=False)["venues"]
        kept_later[sport] = len(venues)
        with gzip.open(later / f"{sport}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(venues, f, ensure_ascii=False, separators=(",", ":"))
    log = {"regions": [r["region"] for r in raws], "dropped": result["dropped"], "later": kept_later}
    (out_dir / "build-log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    return result["stats"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge region extracts into the tennis venue catalogue.")
    parser.add_argument("raw_dir")
    parser.add_argument("out_dir")
    parser.add_argument("--osm-date", default=dt.date.today().isoformat())
    args = parser.parse_args(argv)
    stats = build(Path(args.raw_dir), Path(args.out_dir), args.osm_date)
    t = stats["totals"]
    print(f"{t['venues']} venues, {t['courts']} courts, {t['indoor_dropped']} indoor pitches dropped, {t['backyard_hidden']} backyard courts hidden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
