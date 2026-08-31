# Manual audio-language audit

`tools/check_audio_languages.py` is a standalone, manually invoked utility. It is not connected to the web application, Docker startup, or a scheduler.

It recursively finds MKV files, inspects every audio stream with FFprobe, samples speech from several positions, detects the spoken language with faster-whisper, and compares that result with the stream's declared language tag.

## Environment

The project-local environment is `.venv`. To recreate it later:

```console
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r tools/audio-language-requirements.txt
```

FFmpeg and FFprobe must be installed on the host.

## Run manually

Quote the path/wildcard so the shell does not expand it before Python receives it:

```console
.venv/bin/python tools/check_audio_languages.py '/media/Movies/*.mkv'
```

The wildcard is applied recursively below its non-wildcard root. These are also valid:

```console
.venv/bin/python tools/check_audio_languages.py '/media/**/*.mkv'
.venv/bin/python tools/check_audio_languages.py '/media/TV/Show Name/*.mkv'
```

The first run downloads the selected model into `models/`. The default is the CPU-oriented `small` model with `int8` computation.

## Output

Every file, audio stream, and sample is recorded in a timestamped `.log` file. A separate timestamped `.txt` file contains one media path per line for files requiring review.

Findings include:

- Declared language differs from detected speech.
- Missing, unknown, or unsupported language tag.
- Detection confidence below the configured threshold.
- Samples disagree about the spoken language.
- No usable speech, no audio streams, or a processing error.

Exit status is `0` when everything matches, `1` when files need review, and `2` for a startup/configuration failure.

## Useful options

```console
.venv/bin/python tools/check_audio_languages.py '/media/**/*.mkv' \
  --model small \
  --samples 3 \
  --sample-seconds 30 \
  --confidence 0.75 \
  --agreement 0.60 \
  --log language-check.log \
  --list files-to-review.txt
```

Use `--model medium` for potentially better accuracy at significantly higher CPU, memory, download, and processing cost.

## Limitations

- Detection identifies spoken language, not country/region. Portuguese speech generally returns `pt`, not `PT` versus `BR`.
- Commentary, songs, silence, mixed-language dialogue, and very short speech can produce uncertain results.
- The script never changes media or metadata; it only reads media and creates reports/model-cache files.
