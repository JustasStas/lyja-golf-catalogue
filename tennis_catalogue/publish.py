"""Stage 3 for tennis: upload a build to R2 under `tennis/v1/`, the manifest
last, as golf's (`golf_catalogue.publish`, whose helpers this reuses).

    python -m tennis_catalogue.publish out-tennis/ [--build-id ...] [--manifest-only]

`later/` (padel and pickleball) and the build log are never uploaded.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import json
import os
import random
import time
from pathlib import Path

from golf_catalogue.publish import (
    DATA_CACHE, KEEP_OLD_BUILDS, MANIFEST_CACHE, _client, _cors, content_type, sha256,
)

PREFIX = "tennis/v1"
LISTED = ("search-index.json.gz", "stats.json")
UNPUBLISHED = ("build-log.json",)
# Some 230,000 venue files. On 2026-11-12, 10 connections managed ~40 files/s
# (too slow for the hour the job then had) and 64 ~80 files/s until R2 began
# answering 5xx ("reduce your concurrent request rate"), which can lock the
# whole bucket, golf's files included. 16 stays well below that.
UPLOAD_THREADS = 16
PROGRESS_EVERY = 10_000
# A throttled or failed write is tried again after 2, 4, 8 ... 60 s.
PUT_ATTEMPTS = 8
RETRY_CODES = {"SlowDown", "ServiceUnavailable", "TooManyRequests", "InternalError", "RequestTimeout"}


def _retryable(error: Exception) -> bool:
    from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError, ReadTimeoutError

    if isinstance(error, (BotoConnectionError, ReadTimeoutError)):
        return True
    if isinstance(error, ClientError):
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return error.response.get("Error", {}).get("Code") in RETRY_CODES or status == 429 or status >= 500
    return False


def put_with_retry(put, *, attempts: int = PUT_ATTEMPTS, sleep=time.sleep) -> None:
    """Calls `put` until it succeeds, backing off on throttling and 5xx."""
    for attempt in range(1, attempts + 1):
        try:
            put()
            return
        except Exception as error:  # noqa: BLE001 - re-raised unless retryable
            if attempt == attempts or not _retryable(error):
                raise
            sleep(min(60.0, 2.0 ** attempt) * random.uniform(0.8, 1.2))


def existing_keys(s3, bucket: str, prefix: str) -> set[str]:
    """Keys already under `prefix`: a rerun of the same build skips them."""
    keys: set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.update(o["Key"] for o in page.get("Contents", []))
    return keys

LICENSE_TEXT = """Lyja tennis venue catalogue

Venue data © OpenStreetMap contributors.
https://www.openstreetmap.org/copyright

This database is made available under the Open Database License (ODbL) 1.0:
https://opendatacommons.org/licenses/odbl/1-0/
Any rights in individual contents of the database are licensed under the
Database Contents License: https://opendatacommons.org/licenses/dbcl/1-0/

It is a Derivative Database of OpenStreetMap. The scripts that extract it
are published with every build:
https://data.lyjaapp.eu/tennis/tools/tennis-catalogue.tar.gz
"""


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
        "venue_path": "venues/{country}/{id}.json",
        "country_path": "countries/{country}.json.gz",
        "countries": [c.removesuffix(".json.gz") for c in countries],
        "counts": {"totals": stats["totals"], "countries": stats["countries"]},
        "files": files,
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
    }


def published_paths(out_dir: Path) -> list[Path]:
    return [
        p for p in sorted(out_dir.rglob("*"))
        if p.is_file() and p.name not in UNPUBLISHED and p.relative_to(out_dir).parts[0] != "later"
    ]


def publish(out_dir: Path, build_id: str, tools: Path | None = None) -> dict:
    s3 = _client(connections=UPLOAD_THREADS)
    bucket = os.environ["R2_BUCKET"]
    base = f"{PREFIX}/builds/{build_id}/"
    paths = published_paths(out_dir)
    # Resuming the same build (same extracts, so the same files): skip what
    # an earlier, interrupted upload already put there.
    done_keys = existing_keys(s3, bucket, base)
    total = len(paths)
    paths = [p for p in paths if base + p.relative_to(out_dir).as_posix() not in done_keys]
    print(f"Uploading {len(paths)} of {total} files under {base} ({total - len(paths)} already there)", flush=True)

    def put(path: Path) -> None:
        # put_object: the venue files are small, so upload_file's transfer
        # manager (its own threads, a HEAD per part) is only overhead.
        key = base + path.relative_to(out_dir).as_posix()
        body = path.read_bytes()
        put_with_retry(lambda: s3.put_object(Bucket=bucket, Key=key, Body=body,
                                             ContentType=content_type(path.name), CacheControl=DATA_CACHE))

    started = time.monotonic()
    with futures.ThreadPoolExecutor(max_workers=UPLOAD_THREADS) as pool:
        for n, done in enumerate(futures.as_completed([pool.submit(put, p) for p in paths]), 1):
            done.result()
            if n % PROGRESS_EVERY == 0:
                print(f"  {n} of {len(paths)} in {time.monotonic() - started:.0f} s", flush=True)
    print(f"Uploaded {len(paths)} files under {base} in {time.monotonic() - started:.0f} s", flush=True)
    if tools is not None:
        s3.upload_file(str(tools), bucket, "tennis/tools/tennis-catalogue.tar.gz",
                       ExtraArgs={"ContentType": "application/gzip", "CacheControl": DATA_CACHE})
    s3.put_object(Bucket=bucket, Key="tennis/LICENSE.txt", Body=LICENSE_TEXT.encode(), ContentType="text/plain; charset=utf-8", CacheControl=DATA_CACHE)
    stats = json.loads((out_dir / "stats.json").read_text(encoding="utf-8"))
    doc = manifest(out_dir, build_id, stats)
    s3.put_object(
        Bucket=bucket, Key=f"{PREFIX}/manifest.json",
        Body=json.dumps(doc, ensure_ascii=False, indent=1).encode(),
        ContentType="application/json; charset=utf-8", CacheControl=MANIFEST_CACHE,
    )
    print(f"Tennis manifest now points at {build_id}")
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
        print(f"Deleted old tennis build {build} ({len(keys)} files)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload a tennis build to R2, manifest last.")
    parser.add_argument("out_dir")
    parser.add_argument("--build-id", default=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%MZ"))
    parser.add_argument("--tools")
    parser.add_argument("--manifest-only", action="store_true")
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
