"""Stage 3: upload a build to Cloudflare R2, the manifest last.

    python -m golf_catalogue.publish out/ --build-id 2026-11-01T0300Z

Every file goes under `golf/v1/builds/{build_id}/`; only then is
`golf/v1/manifest.json` written, pointing at that prefix. Until the manifest
changes, readers see the previous build in full, so a half-finished upload is
never live. The two newest builds before this one are kept for readers that
fetched the old manifest a moment ago; older ones are deleted.

Credentials come from the environment (GitHub Actions secrets), never
arguments: CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
R2_BUCKET.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

PREFIX = "golf/v1"
DATA_CACHE = "public, max-age=86400"
MANIFEST_CACHE = "public, max-age=300"
KEEP_OLD_BUILDS = 2
# Published at the top of the build; course and country files are listed by
# pattern instead, there are tens of thousands.
LISTED = ("search-index.json.gz", "stats.json")
CORS_ORIGINS = ["https://lyjaapp.eu", "https://www.lyjaapp.eu", "http://localhost:3000"]

LICENSE_TEXT = """Lyja golf course catalogue

Course data © OpenStreetMap contributors.
https://www.openstreetmap.org/copyright

This database is made available under the Open Database License (ODbL) 1.0:
https://opendatacommons.org/licenses/odbl/1-0/
Any rights in individual contents of the database are licensed under the
Database Contents License: https://opendatacommons.org/licenses/dbcl/1-0/

It is a Derivative Database of OpenStreetMap. The scripts that extract it
are published with every build:
https://data.lyjaapp.eu/golf/tools/golf-catalogue.tar.gz
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def content_type(name: str) -> str:
    if name.endswith(".json.gz"):
        return "application/gzip"
    if name.endswith(".json"):
        return "application/json; charset=utf-8"
    return "text/plain; charset=utf-8"


def manifest(out_dir: Path, build_id: str, stats: dict) -> dict:
    files = []
    for name in LISTED:
        path = out_dir / name
        files.append({"path": name, "sha256": sha256(path), "bytes": path.stat().st_size})
    countries = sorted(p.name for p in (out_dir / "countries").glob("*.json.gz"))
    return {
        "schema": 1,
        "build_id": build_id,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "osm_version_date": stats.get("osm_version_date"),
        "base": f"builds/{build_id}/",
        "course_path": "courses/{country}/{id}.json",
        "country_path": "countries/{country}.json.gz",
        "countries": [c.removesuffix(".json.gz") for c in countries],
        "counts": {"totals": stats["totals"], "countries": stats["countries"]},
        "files": files,
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
    }


def _client():
    import boto3  # imported here so the build and tests need no AWS SDK

    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def publish(out_dir: Path, build_id: str, tools: Path | None = None) -> dict:
    s3 = _client()
    bucket = os.environ["R2_BUCKET"]
    base = f"{PREFIX}/builds/{build_id}/"
    paths = [p for p in sorted(out_dir.rglob("*")) if p.is_file() and p.name != "build-log.json"]

    def put(path: Path) -> None:
        key = base + path.relative_to(out_dir).as_posix()
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type(path.name), "CacheControl": DATA_CACHE})

    with futures.ThreadPoolExecutor(max_workers=32) as pool:
        for done in futures.as_completed([pool.submit(put, p) for p in paths]):
            done.result()  # re-raises: no manifest after a failed upload
    print(f"Uploaded {len(paths)} files under {base}")

    if tools is not None:
        # The extraction scripts beside the data, as ODbL asks of a
        # Derivative Database's maker (this repository is public too).
        s3.upload_file(str(tools), bucket, "golf/tools/golf-catalogue.tar.gz",
                       ExtraArgs={"ContentType": "application/gzip", "CacheControl": DATA_CACHE})
    s3.put_object(Bucket=bucket, Key="golf/LICENSE.txt", Body=LICENSE_TEXT.encode(), ContentType="text/plain; charset=utf-8", CacheControl=DATA_CACHE)
    stats = json.loads((out_dir / "stats.json").read_text(encoding="utf-8"))
    doc = manifest(out_dir, build_id, stats)
    s3.put_object(
        Bucket=bucket, Key=f"{PREFIX}/manifest.json",
        Body=json.dumps(doc, ensure_ascii=False, indent=1).encode(),
        ContentType="application/json; charset=utf-8", CacheControl=MANIFEST_CACHE,
    )
    print(f"Manifest now points at {build_id}")
    _prune(s3, bucket, build_id)
    _cors(s3, bucket)
    return doc


def _prune(s3, bucket: str, current: str) -> None:
    builds = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{PREFIX}/builds/", Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            builds.add(p["Prefix"].rstrip("/").rsplit("/", 1)[-1])
    older = sorted(b for b in builds if b != current)
    for build in older[:max(len(older) - KEEP_OLD_BUILDS, 0)]:
        keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{PREFIX}/builds/{build}/"):
            keys += [{"Key": o["Key"]} for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[i:i + 1000], "Quiet": True})
        print(f"Deleted old build {build} ({len(keys)} files)")


def _cors(s3, bucket: str) -> None:
    """The website's /data/golf page reads the manifest and stats in the
    browser. A token scoped to objects may not be allowed to set this; the
    rule can then be added once in the R2 dashboard (see README)."""
    try:
        s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [
            {"AllowedOrigins": CORS_ORIGINS, "AllowedMethods": ["GET", "HEAD"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 86400},
        ]})
    except Exception as error:  # noqa: BLE001 - informational only
        print(f"::warning::Could not set the bucket's CORS rule ({error.__class__.__name__}); add it in the R2 dashboard.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload a build to R2, manifest last.")
    parser.add_argument("out_dir")
    parser.add_argument("--build-id", default=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%MZ"))
    parser.add_argument("--tools", help="tar.gz of tools/golf-catalogue, published beside the data")
    parser.add_argument("--manifest-only", action="store_true", help="write manifest.json locally, upload nothing")
    args = parser.parse_args(argv)
    out = Path(args.out_dir)
    if args.manifest_only:
        stats = json.loads((out / "stats.json").read_text(encoding="utf-8"))
        (out / "manifest.json").write_text(json.dumps(manifest(out, args.build_id, stats), indent=1), encoding="utf-8")
        return 0
    publish(out, args.build_id, Path(args.tools) if args.tools else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
