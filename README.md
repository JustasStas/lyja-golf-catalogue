# Lyja golf course catalogue

Course data © OpenStreetMap contributors, available under the Open Database
Licence (ODbL 1.0): <https://www.openstreetmap.org/copyright>. The published
catalogue is a Derivative Database under the same licence; this repository
is the means of producing it.

Lyja's worldwide golf course catalogue, built monthly from OpenStreetMap and
served from Cloudflare R2 at `https://data.lyjaapp.eu/golf/`. The app
downloads the search index once (Lyja Pro golf only) and one file per saved
course; search, nearest course and scoring all run on the phone.

## What it is built from

OpenStreetMap only: `leisure=golf_course` outlines (nodes as a fallback) and
`golf=hole` ways, grouped by `golf=course` relations, `golf:course:name` or
hole numbering. Driving ranges, mini golf, disc golf and outlines under 3 ha
without holes are left out. No other course data is ever mixed in: the files
are a Derivative Database under the ODbL.

## How a run works (`.github/workflows/golf-catalogue.yml`)

1. **plan**: tests, then the region matrix from `regions.yaml` (Geofabrik
   extracts with their ISO country).
2. **extract** (one job per region): download the `.osm.pbf`, check its md5,
   `osmium tags-filter … nwr/leisure=golf_course nwr/golf r/type=sport`,
   delete the download, reduce to a small JSON (`golf_catalogue.extract`).
3. **build**: merge every region (`golf_catalogue.build`), compare with the
   live manifest (`golf_catalogue.guard`: more than 20 % fewer courses in a
   country of 20 or more, or 5 % fewer worldwide, stops the run), write the
   run summary, and publish (`golf_catalogue.publish`): every file under
   `golf/v1/builds/{build_id}/`, then `golf/v1/manifest.json` last.

Schedule: the 1st of every month at 03:00 UTC, and by hand from the Actions
tab (with an optional region filter for a test; a partial build is never
published).

## Published files

```
golf/v1/manifest.json                          build id, counts, base path, file hashes
golf/v1/builds/{build}/search-index.json.gz    every course: id, name, country, centroid, holes, hole data flag
golf/v1/builds/{build}/courses/{CC}/{id}.json  one course with its outline, layouts and holes
golf/v1/builds/{build}/countries/{CC}.json.gz  every course of a country
golf/v1/builds/{build}/stats.json              per-country coverage
golf/LICENSE.txt                               ODbL notice and attribution
golf/tools/golf-catalogue.tar.gz               these scripts
```

Each course file may carry an `outline`: the `leisure=golf_course` area's
outer rings as `[[[lat, lon], …], …]`, largest first. A multipolygon keeps
every outer ring of at least 5 % of the largest one (at most 8), so a course
split by a road keeps both halves; inner rings are dropped. Each ring is
Douglas–Peucker simplified in a local metric frame, starting at a 4 m
tolerance and doubling until all rings together fit in 96 points, then
rounded to 5 decimals (~1 m). Rings are implicitly closed (the first point
is not repeated). A course mapped as a node has no outline. Adding the field
is backward compatible: v1 readers that do not know it ignore it.

Readers start from the manifest: `base` + `files[].path`, or `base` +
`course_path` / `country_path` with `{country}` and `{id}` filled in. JSON
Schemas are in `schema/`. `v1` is the schema version: a breaking change goes
to `v2` and `v1` stays served until the app stops reading it.

## One-time setup (Cloudflare and GitHub)

1. Cloudflare → R2 → create a bucket, `lyja-golf`.
2. The bucket → Settings → Custom domains → add `data.lyjaapp.eu`.
3. R2 → Manage API tokens → create a token with **Object Read & Write** on
   that bucket only. It shows an Access Key ID and a Secret Access Key.
4. GitHub → this repository → Settings → Secrets and variables → Actions:
   - secrets `CLOUDFLARE_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
   - variable `R2_BUCKET` if the bucket is not called `lyja-golf`
5. The bucket → Settings → CORS policy, so `/data/golf` can read the
   manifest in the browser (the publish step tries to set this itself, which
   an object-only token may not be allowed to do):

   ```json
   [{"AllowedOrigins": ["https://lyjaapp.eu", "https://www.lyjaapp.eu"],
     "AllowedMethods": ["GET", "HEAD"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 86400}]
   ```

## Local development

```bash
pip install -r requirements.txt
python -m pytest -q                       # small fixture courses in tests/fixtures
python tests/fixtures/make_fixtures.py    # after changing a fixture

# One region by hand (needs osmium-tool):
curl -LO https://download.geofabrik.de/europe/lithuania-latest.osm.pbf
osmium tags-filter lithuania-latest.osm.pbf nwr/leisure=golf_course nwr/golf r/type=sport -o golf.osm.pbf
python -m golf_catalogue.extract golf.osm.pbf --region lithuania --country LT --out raw/lithuania.json.gz
python -m golf_catalogue.build raw out
python -m golf_catalogue.summary out
```

The layout rules follow the edge cases handled by
[henrikdav/osm-golf-courses](https://github.com/henrikdav/osm-golf-courses)
(MIT); no code or output was copied from it.

Course data © OpenStreetMap contributors, ODbL.

## Tennis venues (`tennis_catalogue/`)

The same monthly run builds Lyja's outdoor tennis venue catalogue, served at
`https://data.lyjaapp.eu/tennis/v1/` (Lyja Pro Outdoor tennis; the app's
`TennisVenue` and `TennisIndexEntry`). Golf's steps, files and schema are
unchanged; each region is still downloaded once.

Per region, after golf's filter:

1. `osmium tags-filter … nwr/sport=tennis,padel,pickleball
   nwr/leisure=pitch,practice_pitch,sports_centre,sports_hall,stadium nwr/club=sport`
2. `tennis_catalogue.boxes`: the area 250 m around every racket-sport pitch.
3. A second pass for the surroundings, kept only inside those areas
   (`osmium extract -p boxes.geojson`): buildings, residential land, parks,
   recreation grounds, main and residential roads, rivers and canals.
4. `tennis_catalogue.extract` reduces both to a small JSON.

Then one job (`tennis_catalogue.build`) applies the rules:

- Courts are `leisure=pitch` with `sport=tennis` (in a `;` list too).
- Dropped as indoor: `indoor=yes`, `covered=yes`, or a pitch inside a
  building or `leisure=sports_hall`. `covered=seasonal` is kept and flagged
  (`covered_share`). Dropped as disused: any `disused:*` or `abandoned:*`
  key. Practice walls (`leisure=practice_pitch`) are counted, not listed.
- A pitch drawn around smaller tennis pitches (double mapping) is dropped.
- Venues: the smallest sports centre, `club=sport`, or named recreation
  ground or park around the courts; otherwise courts within 40 m of each
  other (edge to edge) that no main road, residential street or waterway
  separates.
- A single unnamed court that is private or in residential land, with no
  club or sports centre around it, is a backyard court: counted, not listed.
- Names: the venue feature's, the commonest court name, a named tennis place
  or club within 100 m, else generated ("Tennis courts, {park or street}",
  `name_generated` and `name_base` for the app to localise).
- Courts per pitch: `courts` / `pitch:count` / `count`, else one under 115 m
  round, else area ÷ 650 m². Surfaces per court: clay is red unless
  `clay:type` or `surface:colour` says green or Har-Tru; hard, acrylic,
  asphalt and concrete are hard; grass; artificial turf; anything else
  unknown.
- Court direction: each pitch's minimum rotated rectangle in a local metric
  frame. A single court's long side is high confidence; several courts side
  by side run along the short side, low confidence. The venue's is the
  court-weighted circular mean, `none` when its courts disagree by over 30°.
- Lights: yes, partial, no or unknown over the courts; access: the commonest.

The guard refuses a build that lost 20 % of a country's venues (of 20 or
more) or 5 % of the world's, and a search index over 6 MB gzipped. Tennis
publishes only when every region's tennis extract is in; padel and
pickleball venues are grouped the same way and kept as a run artifact
(`tennis-report`, `later/`) for later activities.

```
tennis/v1/manifest.json                          build id, counts, base, venue_path, file hashes
tennis/v1/builds/{build}/search-index.json.gz    every outdoor venue: id, name, country, centroid, courts, surface, lights, access
tennis/v1/builds/{build}/venues/{CC}/{id}.json   one venue
tennis/v1/builds/{build}/countries/{CC}.json.gz  every venue of a country
tennis/v1/builds/{build}/stats.json              per country: raw objects, pitches, indoor dropped, backyard hidden, venues, % with surface, lit, access, axis
tennis/LICENSE.txt, tennis/tools/tennis-catalogue.tar.gz
```

Schemas: `schema/tennis-*.schema.json`. Fixtures:
`python tests/fixtures/make_tennis_fixtures.py`.
