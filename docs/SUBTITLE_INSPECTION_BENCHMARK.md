# Subtitle inspection bounded benchmark

Date: 2026-09-18

This benchmark is read-only. It sampled six existing media streams, selected for codec diversity, and never wrote detector rows or queued work. Embedded extraction was limited to 120 seconds per stream with a 20-second command timeout.

## Observed sample

| Codec | Samples | Result |
| --- | ---: | --- |
| SubRip/SRT | 4 | 4 extracted successfully; 0.275–1.429 s each |
| HDMV PGS | 1 | Correctly reported as unsupported text extraction |
| VobSub | 1 | Correctly reported as unsupported text extraction |

Total elapsed: 3.195 seconds; mean 0.532 seconds per sampled stream; median 0.336 seconds; approximately 1.878 streams/second for this sample.

One sampled SRT was flagged as possible mojibake and was not treated as a healthy language match. Sparse and low-text samples remained low-confidence, as intended. Graphical subtitles were not forced through a text detector.

## Conclusions

- The bounded extraction path is fast enough for incremental work.
- Text and graphical codecs are separated safely.
- No preview cache or full-catalog run is justified by this benchmark.
- Parallelism should remain conservative until a larger user-approved benchmark is requested.

The benchmark command is `scripts/subtitle_inspection_benchmark.py`; it remains bounded and read-only.
