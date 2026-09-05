# Subtitle corruption audit

`tools/find_corrupt_subtitles.py` is a read-only, standalone tool for finding embedded text subtitle tracks that contain Unicode replacement characters (`�`). This is the characteristic damage produced when legacy text was decoded with the wrong encoding and the original byte was discarded.

It requires Python 3 and FFprobe. It has no Python package dependencies, so the project's `.venv` can run it without another installation step.

## Run manually

Quote the path/wildcard so the shell does not expand it:

```console
.venv/bin/python tools/find_corrupt_subtitles.py '/media/**/*.mkv'
```

For the media mounts exposed only inside VideoStreamEdit's container:

```console
docker exec -it videostreamedit python /app/tools/find_corrupt_subtitles.py '/media/**/*.mkv'
```

The Docker image must include the `tools` directory for that second form. When running from the project checkout, use the host `.venv` and a host-visible media path.

## Output and resume

- `subtitle-corruption-findings.tsv` lists each affected media file, subtitle number, FFprobe stream index, codec, replacement count, and an example.
- A timestamped `.log` records progress, findings, and errors.
- `subtitle-corruption-audit.state.jsonl` checkpoints every completed file.

Rerunning the same command skips files whose size and modification time have not changed. Use `--fresh` to scan everything again. Results are written during the run, so an interruption does not lose completed work.

Useful options:

```console
.venv/bin/python tools/find_corrupt_subtitles.py '/media/**/*.mkv' \
  --list corrupt-subtitles.tsv \
  --state corrupt-subtitles.state.jsonl \
  --log corrupt-subtitles.log \
  --progress-every 10
```

The tool does not modify or repair subtitles. Image-based subtitle tracks are ignored because they do not contain encoded text.
