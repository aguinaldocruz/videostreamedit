# Preview-cache benchmark protocol

This protocol is intentionally bounded. It never means “run over the catalog.”

## Fixture selection

Choose at most five representative files manually:

- one Matroska file with a common audio codec;
- one file with a less seek-friendly codec/container;
- one file with multiple audio streams;
- one short file;
- one long file.

Use explicit paths. Do not pass a library root or an unbounded recursive pattern.

## Sequence

1. Run `--dry-run` and verify files, stream index, segment commands, and output path.
2. Run one worker with two segments per file and save JSON.
3. Repeat the same small set to measure the extraction baseline again.
4. Run `--duplicate --workers 2` for one file/segment to observe duplicate pressure.
5. Run the acceptance gate on non-dry-run reports.
6. Compare latency, failures, bytes, and CPU/memory externally from Docker metrics.

Example:

```bash
python scripts/preview_cache_benchmark.py /media/test/a.mkv /media/test/b.mkv --limit 2 --segments 2 --dry-run
python scripts/preview_cache_benchmark.py /media/test/a.mkv /media/test/b.mkv --limit 2 --segments 2 --output /tmp/preview-cold.json
python scripts/preview_cache_gate.py /tmp/preview-cold.json
python scripts/preview_cache_benchmark.py /media/test/a.mkv --limit 1 --segments 1 --workers 2 --duplicate --output /tmp/preview-duplicate.json
```

## Initial acceptance thresholds

- 0 failed extractions;
- average extraction latency at or below 8 seconds;
- average segment size at or below 4 MB;
- no sustained CPU saturation outside the preview process;
- no measurable delay to interactive edits or queue workers;
- duplicate pressure must not create more than one durable cache file for the same key.

These are starting gates, not permanent guarantees. Adjust only after measured evidence.

## Production decision

Keep `PREVIEW_MAX_CONCURRENT=1` unless the duplicate and resource measurements show a safe increase. Do not run a full preview prewarm. The cache remains disposable and on-demand.
