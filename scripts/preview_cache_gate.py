#!/usr/bin/env python3
"""Evaluate a bounded preview benchmark JSON report.

The gate is read-only and never invokes FFmpeg. It is intentionally conservative
so production defaults are not increased without measured evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--max-average-seconds", type=float, default=8.0)
    parser.add_argument("--max-failure-rate", type=float, default=0.05)
    parser.add_argument("--max-average-bytes", type=int, default=4_000_000)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read benchmark report: {exc}", file=sys.stderr)
        return 2
    jobs = int(report.get("jobs") or 0)
    successful = int(report.get("successful") or 0)
    failed = int(report.get("failed") or 0)
    average_seconds = report.get("average_extraction_seconds")
    average_bytes = report.get("average_bytes")
    checks = {
        "has_jobs": jobs > 0,
        "all_jobs_completed": jobs == successful + failed,
        "failure_rate": jobs > 0 and failed / jobs <= args.max_failure_rate,
        "average_latency": average_seconds is not None and float(average_seconds) <= args.max_average_seconds,
        "average_size": average_bytes is not None and int(average_bytes) <= args.max_average_bytes,
    }
    result = {"passed": all(checks.values()), "checks": checks, "report": str(args.report), "limits": {"max_average_seconds": args.max_average_seconds, "max_failure_rate": args.max_failure_rate, "max_average_bytes": args.max_average_bytes}}
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
