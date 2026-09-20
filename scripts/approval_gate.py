#!/usr/bin/env python3
"""Read-only functional approval gate for the redesigned VideoStreamEdit UI."""
from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

PATHS = (
    "/api/v19/movies",
    "/api/v19/tv/summary",
    "/api/v19/tv",
    "/api/v19/reports/image-subtitles?kind=movies",
    "/api/v19/reports/image-subtitles?kind=tv",
    "/api/v19/reports/html-subtitles?kind=movies",
    "/api/v19/reports/damaged-subtitles?kind=movies",
    "/api/v86/dashboard/stats",
    "/api/v65/queue?limit=1",
    "/api/v86/workflow-read-model?limit=1",
)


def check(base: str, path: str, timeout: float) -> tuple[int, int, float]:
    request = Request(base.rstrip("/") + path, headers={"Accept": "application/json"})
    started = time.monotonic()
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
        return int(response.status), len(body), time.monotonic() - started


def check_tv_selection(base: str, timeout: float) -> tuple[int, int, float]:
    started = time.monotonic()
    request = Request(base.rstrip("/") + "/api/v19/tv/summary", headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        summary = json.loads(response.read())
    if not summary or not isinstance(summary, list) or not summary[0].get("id"):
        raise ValueError("TV summary did not provide a selectable show")
    show_id = quote(str(summary[0]["id"]), safe="")
    request = Request(base.rstrip("/") + "/api/v19/tv?show_id=" + show_id, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        details = json.loads(response.read())
        status = int(response.status)
    if status != 200 or not details or not details[0].get("seasons"):
        raise ValueError("Scoped TV detail did not return seasons")
    return status, len(json.dumps(details)), time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8383")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-seconds", type=float, default=10.0)
    args = parser.parse_args()
    failures = []
    for path in PATHS:
        try:
            status, size, elapsed = check(args.base_url, path, args.timeout)
            if status != 200:
                failures.append(f"{path}: HTTP {status}")
            elif elapsed > args.max_seconds:
                failures.append(f"{path}: {elapsed:.2f}s exceeds {args.max_seconds:.2f}s")
            elif size == 0:
                failures.append(f"{path}: empty response")
            else:
                print(f"OK   {path} - HTTP {status} - {size} bytes - {elapsed:.2f}s")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            failures.append(f"{path}: {exc}")
    try:
        status, size, elapsed = check_tv_selection(args.base_url, args.timeout)
        if elapsed > args.max_seconds:
            failures.append(f"TV scoped selection: {elapsed:.2f}s exceeds {args.max_seconds:.2f}s")
        else:
            print(f"OK   TV scoped selection - HTTP {status} - {size} bytes - {elapsed:.2f}s")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        failures.append(f"TV scoped selection: {exc}")
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("Functional approval gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
