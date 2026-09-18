#!/usr/bin/env python3
"""Read-only deployment smoke check for VideoStreamEdit.

Usage:
    python scripts/production_smoke.py --base-url http://127.0.0.1:8383
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch(base: str, path: str, timeout: float) -> tuple[int, bytes, float]:
    request = Request(base.rstrip("/") + path, headers={"Accept": "application/json,text/html,*/*"})
    started = time.monotonic()
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
    return int(response.status), body, (time.monotonic() - started) * 1000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8383")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-endpoint timeout in seconds")
    args = parser.parse_args()
    checks = [
        ("/api/health", "health"),
        ("/", "page"),
        ("/assets/v19.css", "asset"),
        ("/assets/v19.js", "asset"),
        ("/api/v86/readiness", "readiness"),
        ("/api/v86/operational-summary", "summary"),
    ]
    failures: list[str] = []
    for path, kind in checks:
        try:
            status, body, elapsed_ms = fetch(args.base_url, path, args.timeout)
            if status != 200:
                failures.append(f"{path}: HTTP {status}")
                continue
            if kind in {"health", "readiness", "summary"}:
                payload = json.loads(body)
                if kind == "health" and payload.get("status") != "ok":
                    failures.append(f"{path}: status={payload.get('status')!r}")
                if kind == "readiness" and payload.get("status") != "ready":
                    failures.append(f"{path}: status={payload.get('status')!r}")
                if kind == "readiness" and set(payload.get("checks", {})) != {
                    "database", "workflow_schema", "read_models"
                }:
                    failures.append(f"{path}: incomplete checks")
                if kind == "summary" and not all(
                    isinstance(payload.get(key), dict)
                    for key in ("tasks", "indexes", "workflows", "read_models")
                ):
                    failures.append(f"{path}: incomplete operational summary")
                print(f"OK   {path} - {payload.get('status', 'summary')} - {elapsed_ms:.0f} ms")
            else:
                if not body:
                    failures.append(f"{path}: empty response")
                print(f"OK   {path} - {len(body)} bytes - {elapsed_ms:.0f} ms")
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            failures.append(f"{path}: {exc}")
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("Smoke check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
