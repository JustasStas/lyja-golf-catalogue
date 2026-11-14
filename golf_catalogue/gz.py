"""Gzip files with the same bytes on every build.

`gzip.open` stamps the time of writing into the header, so two builds of the
same data differ byte for byte, and so do their sha256s. On 2026-11-13 a
resumed tennis upload kept the search index an interrupted attempt had put
in R2, while the manifest carried the new build's checksum: the app refused
the index ("could not load the list of courts"). Written with mtime 0 and no
file name, a file is a function of its contents alone.
"""

from __future__ import annotations

import contextlib
import gzip
import io
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def open_text(path: Path | str) -> Iterator[io.TextIOWrapper]:
    """A text stream writing gzip with a fixed header (mtime 0, no name)."""
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
        stream = io.TextIOWrapper(zipped, encoding="utf-8")
        try:
            yield stream
        finally:
            stream.flush()
            stream.detach()
