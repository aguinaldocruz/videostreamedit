#!/usr/bin/env python3
"""Read-only latency and payload gate for production redesign endpoints."""
from __future__ import annotations

import argparse
import gzip
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TARGETS = (
    ("health", "/api/health", 1.0),
    ("readiness", "/api/v86/readiness", 2.0),
    ("operational summary", "/api/v86/operational-summary", 2.0),
    ("TV summary", "/api/v19/tv/summary", 2.0),
    ("movies", "/api/v19/movies", 10.0),
    ("dashboard", "/api/v86/dashboard/stats", 5.0),
)


def check(base: str, path: str, timeout: float) -> tuple[int, int, int, float]:
    started = time.monotonic()
    request = Request(base.rstrip("/") + path, headers={"Accept": "application/json", "Accept-Encoding": "gzip"})
    with urlopen(request, timeout=timeout) as response:
        wire = response.read()
        body = gzip.decompress(wire) if response.headers.get("Content-Encoding") == "gzip" else wire
        return int(response.status), len(wire), len(body), time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8383")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    failures = []
    for label, path, limit in TARGETS:
        try:
            status, wire, body, elapsed = check(args.base_url, path, args.timeout)
            if status != 200:
                failures.append(f"{label}: HTTP {status}")
            elif body == 0:
                failures.append(f"{label}: empty response")
            elif elapsed > limit:
                failures.append(f"{label}: {elapsed:.2f}s > {limit:.2f}s")
            else:
                print(f"OK   {label} - {elapsed:.2f}s - {wire} wire bytes / {body} payload bytes")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, gzip.BadGzipFile) as exc:
            failures.append(f"{label}: {exc}")
    if failures:
        for failure in failures:
            print("FAIL " + failure, file=sys.stderr)
        return 1
    print("Performance gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
