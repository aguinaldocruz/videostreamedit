#!/usr/bin/env python3
"""Verify service recovery after an externally performed restart.

This script never restarts containers and never mutates queue or media state.
Run it after a docker compose restart videostreamedit command.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_json(base: str, path: str, timeout: float) -> dict:
    request = Request(base.rstrip("/") + path, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{path}: HTTP {response.status}")
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path}: response is not an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8383")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--wait-seconds", type=float, default=120.0)
    args = parser.parse_args()
    deadline = time.monotonic() + max(1.0, args.wait_seconds)
    last_error = "service did not become ready"
    while time.monotonic() < deadline:
        try:
            readiness = get_json(args.base_url, "/api/v86/readiness", args.timeout)
            if readiness.get("status") != "ready":
                last_error = f"readiness={readiness.get('status')!r}"
            else:
                summary = get_json(args.base_url, "/api/v86/operational-summary", args.timeout)
                if not all(isinstance(summary.get(key), dict) for key in ("tasks", "indexes", "workflows", "read_models")):
                    raise RuntimeError("operational summary shape is incomplete")
                print("Recovery check passed: readiness and operational summary are available.")
                return 0
        except (HTTPError, URLError, TimeoutError, ValueError, OSError, RuntimeError) as exc:
            last_error = str(exc)
        time.sleep(2)
    print(f"Recovery check failed: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
