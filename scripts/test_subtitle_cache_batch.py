"""Verify one-pass extraction, track identity and isolated retry behavior."""

import ast
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path


if not shutil.which("ffmpeg"):
    raise SystemExit("ffmpeg is required for the subtitle batch test")

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from app.subtitle_text_decode import decode_complete_srt
cache_tree = ast.parse((root / "app/subtitle_cache.py").read_text())
track_type = next(node for node in cache_tree.body if isinstance(node, ast.ClassDef) and node.name == "TextSubtitle")
worker_tree = ast.parse((root / "app/subtitle_cache_worker.py").read_text())
batch = next(node for node in worker_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_extract_embedded_batch")
scope = {"dataclass": __import__("dataclasses").dataclass, "Path": Path,
         "decode_complete_srt": decode_complete_srt,
         "subprocess": subprocess, "tempfile": tempfile, "MAX_TEXT_BYTES": 32 * 1024 * 1024}
exec(compile(ast.Module(body=[track_type, batch], type_ignores=[]), "subtitle-cache-batch", "exec"), scope)

with tempfile.TemporaryDirectory() as folder:
    directory = Path(folder)
    first, second = directory / "first.srt", directory / "second.srt"
    first.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish line\n", encoding="utf-8")
    second.write_text("1\n00:00:00,000 --> 00:00:01,000\nLinha em português\n", encoding="utf-8")
    media = directory / "fixture.mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "srt", "-i", str(first),
                    "-f", "srt", "-i", str(second), "-map", "0:s:0", "-map", "1:s:0",
                    "-c:s", "srt", str(media)], check=True)
    tracks = [{"source": "embedded", "type_index": n, "external_path": "", "codec": "subrip"} for n in (0, 1)]
    original_run = subprocess.run
    ffmpeg_calls = []

    def recording_run(command, **kwargs):
        ffmpeg_calls.append(command)
        return original_run(command, **kwargs)

    scope["subprocess"] = type("Process", (), {"run": staticmethod(recording_run),
                                                 "DEVNULL": subprocess.DEVNULL, "PIPE": subprocess.PIPE,
                                                 "TimeoutExpired": subprocess.TimeoutExpired})
    result = scope["_extract_embedded_batch"](media, tracks)
    assert len(ffmpeg_calls) == 1 and sorted(result) == [0, 1], (ffmpeg_calls, result)
    assert "English line" in result[0].text and "Linha em português" in result[1].text
    assert not list(directory.glob("vse-subtitle-batch-*")), "Temporary outputs were not removed"
    bad = [{**tracks[0], "type_index": 99}, tracks[1]]
    assert scope["_extract_embedded_batch"](media, bad) == {}, "Failed batch must not accept partial output"

print("PASS: two subtitle tracks extracted in one FFmpeg invocation; failed batch rejected")
