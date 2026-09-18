#!/usr/bin/env python3
"""Bounded local audio-preview benchmark.

This tool never discovers a catalog. Pass explicit files or glob patterns and
use --limit to keep the run small. It measures cold FFmpeg extraction and,
optionally, concurrent duplicate requests against the same segment.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import time
from pathlib import Path

SAMPLE_SECONDS = 25
ANCHOR_SECONDS = 5 * 60
AUDIO_RATE = 32000
CHANNELS = 1
BITRATE = "64k"


def expand(values: list[str], limit: int) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = Path(value)
        candidates = sorted(path.parent.rglob(path.name)) if any(ch in value for ch in "*?[") else [path]
        for candidate in candidates:
            if candidate.is_file() and str(candidate.resolve()) not in seen:
                seen.add(str(candidate.resolve()))
                found.append(candidate.resolve())
                if len(found) >= limit:
                    return found
    return found


def command(path: Path, stream: int, segment: int) -> list[str]:
    return [
        "nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", str(max(0, segment) * SAMPLE_SECONDS), "-i", str(path),
        "-map", f"0:a:{stream}", "-vn", "-t", str(SAMPLE_SECONDS),
        "-ac", str(CHANNELS), "-ar", str(AUDIO_RATE), "-b:a", BITRATE,
        "-f", "mp3", "pipe:1",
    ]


def extract(path: Path, stream: int, segment: int) -> dict:
    started = time.perf_counter()
    try:
        result = subprocess.run(command(path, stream, segment), capture_output=True, timeout=180, check=True)
        return {"ok": True, "bytes": len(result.stdout), "seconds": time.perf_counter() - started}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "bytes": 0, "seconds": time.perf_counter() - started, "error": str(exc)[:500]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Explicit media paths or bounded glob patterns")
    parser.add_argument("--limit", type=int, default=3, help="Maximum media files (default: 3)")
    parser.add_argument("--segments", type=int, default=2, help="Segments per file (default: 2)")
    parser.add_argument("--stream", type=int, default=0, help="Audio stream index (default: 0)")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent extractions (default: 1)")
    parser.add_argument("--duplicate", action="store_true", help="Repeat one segment concurrently to measure duplicate pressure")
    parser.add_argument("--output", type=Path, help="Write the JSON report to this file")
    parser.add_argument("--dry-run", action="store_true", help="Validate selected files and print commands without invoking FFmpeg")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 50 or args.segments < 1 or args.segments > 12 or args.workers < 1 or args.workers > 4:
        parser.error("limit 1..50, segments 1..12, workers 1..4")
    if shutil.which("ffmpeg") is None:
        parser.error("ffmpeg is required")
    files = expand(args.paths, args.limit)
    if not files:
        parser.error("No explicit media files matched")
    jobs = [(path, args.stream, segment) for path in files for segment in range(args.segments)]
    if args.duplicate:
        jobs = [jobs[0]] * max(2, args.workers)
    started = time.perf_counter()
    if args.dry_run:
        results = [{"ok": True, "bytes": 0, "seconds": 0, "command": command(*item)} for item in jobs]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(lambda item: extract(*item), jobs))
    elapsed = time.perf_counter() - started
    successful = [item for item in results if item["ok"]]
    report = {
        "files": [str(path) for path in files],
        "jobs": len(jobs),
        "workers": args.workers,
        "duplicate_mode": args.duplicate,
        "dry_run": args.dry_run,
        "elapsed_seconds": round(elapsed, 3),
        "successful": len(successful),
        "failed": len(results) - len(successful),
        "average_extraction_seconds": round(sum(item["seconds"] for item in successful) / len(successful), 3) if successful else None,
        "average_bytes": round(sum(item["bytes"] for item in successful) / len(successful)) if successful else None,
        "results": results,
    }
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if len(successful) == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
