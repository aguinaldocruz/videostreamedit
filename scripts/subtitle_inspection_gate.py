#!/usr/bin/env python3
"""Read-only regression gate for subtitle inspection; never queues work."""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=30) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8383")
    args = parser.parse_args()
    status = get(args.base, "/api/v80/setup/index/subtitles/status")
    assert all(key in status for key in ("running", "queued", "failed", "completed", "indexed")), status
    for kind in ("movies", "tv"):
        report = get(args.base, f"/api/v19/reports/subtitle-no-confidence?{urllib.parse.urlencode({'kind': kind, 'status': 'no_confidence'})}")
        assert report.get("kind") == kind, report
        assert isinstance(report.get("items"), list), report
    print(json.dumps({"inspection": {k: status.get(k) for k in ("running", "queued", "failed", "completed", "indexed", "total")}, "reports": "available"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
