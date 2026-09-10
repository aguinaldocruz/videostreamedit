from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import app.v54 as jobs
import app.v63 as cache
from app.v2 import probe
from app.v11 import connection
from app.v63 import app


logger = logging.getLogger("uvicorn.error")


def preview_index_with_encoding_fallback(item: dict) -> None:
    path = Path(item["path"])
    folder = jobs.cache_folder(str(path))
    temporary = folder.with_name(folder.name + ".building")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True, exist_ok=True)
    details = probe(path)
    try:
        duration = float((details.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    (temporary / "duration.txt").write_text(str(duration), encoding="ascii")
    audio_index = audio_files = 0
    try:
        for stream in details.get("streams", []):
            kind = stream.get("codec_type")
            if kind == "audio":
                initial_segment = 12 if duration > 300 else max(0, int(max(duration - 1, 0) // 25))
                data = cache.encode_audio(path, audio_index, initial_segment)
                if data:
                    (temporary / f"audio-{audio_index}-{initial_segment}.mp3").write_bytes(data)
                    audio_files += 1
                audio_index += 1
        shutil.rmtree(folder, ignore_errors=True)
        temporary.rename(folder)
        files = [file for file in folder.iterdir() if file.is_file() and file.name != "duration.txt"]
        with connection() as db:
            db.execute("DELETE FROM preview_cache_files WHERE path=?", (str(path),))
            db.execute(
                "INSERT OR REPLACE INTO preview_cache_index(path,modified,size,audio_files,subtitle_files,cache_bytes,indexed_at) VALUES(?,?,?,?,?,?,datetime('now'))",
                (str(path), item["modified"], item["size"], audio_files, 0, sum(file.stat().st_size for file in files)),
            )
        for file in files:
            cache.register_file(str(path), file, True)
        cache.enforce_lru()
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


jobs.processors["previews"] = preview_index_with_encoding_fallback
