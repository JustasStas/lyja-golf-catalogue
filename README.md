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
golf/v1/builds/{build}/courses/{CC}/{id}.json  one course with its layouts and holes
golf/v1/builds/{build}/countries/{CC}.json.gz  every course of a country
golf/v1/builds/{build}/stats.json              per-country coverage
golf/LICENSE.txt                               ODbL notice and attribution
golf/tools/golf-catalogue.tar.gz               these scripts
```

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
