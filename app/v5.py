from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.v2 import TYPE_SPECIFIER, app, authorized_file, make_language, probe

STATIC_DIR = Path(__file__).parent / "static"
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}


class TrackChange(BaseModel):
    codec_type: Literal["audio", "subtitle"]
    type_index: int = Field(ge=0)
    language: str | None = None
    region: str | None = None
    title: str | None = None


class ExternalSubtitleChange(BaseModel):
    path: str
    embed: bool = False
    language: str = ""
    region: str = ""
    title: str = ""
    forced: bool = False


class SingleEditRequest(BaseModel):
    path: str
    tracks: list[TrackChange] = []
    external_subtitles: list[ExternalSubtitleChange] = []
    forced_audio: int | None = Field(default=None, ge=0)
    forced_subtitle: int | None = Field(default=None, ge=0)


@app.middleware("http")
async def v5_assets(request: Request, call_next):
    if request.method == "GET":
        assets = {
            "/": ("v5.html", "text/html"),
            "/app.css": ("v5.css", "text/css"),
            "/app.js": ("v5.js", "text/javascript"),
            "/previous.css": ("v4.css", "text/css"),
        }
        if request.url.path in assets:
            filename, media_type = assets[request.url.path]
            return FileResponse(STATIC_DIR / filename, media_type=media_type)
    return await call_next(request)


def split_tag(value: str) -> tuple[str, str]:
    match = re.match(r"^([A-Za-z]{2,3})(?:[-_]([A-Za-z]{2}|\d{3}))?$", value.strip())
    return (match.group(1).lower(), (match.group(2) or "").upper()) if match else (value.strip(), "")


def external_subtitles(media: Path) -> list[dict]:
    found = []
    prefix = media.stem.casefold()
    for candidate in sorted(media.parent.iterdir(), key=lambda path: path.name.casefold()):
        if not candidate.is_file() or candidate.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        candidate_stem = candidate.stem
        if candidate_stem.casefold() != prefix and not candidate_stem.casefold().startswith(prefix + "."):
            continue
        suffix = candidate_stem[len(media.stem):].lstrip(".")
        tokens = [token for token in re.split(r"[._ -]+", suffix) if token]
        forced = any(token.casefold() == "forced" for token in tokens)
        language, region = "", ""
        for token in tokens:
            if token.casefold() == "forced":
                continue
            parsed_language, parsed_region = split_tag(token)
            if re.match(r"^[a-z]{2,3}$", parsed_language):
                language, region = parsed_language, parsed_region
                break
        found.append({"path": str(candidate.resolve()), "name": candidate.name, "codec_type": "subtitle", "codec": candidate.suffix.lower().lstrip("."), "language": language, "region": region, "title": "", "forced": forced, "external": True})
    return found


@app.get("/api/media/details")
def media_details(path: str) -> dict:
    media = authorized_file(path)
    counters = {"audio": 0, "subtitle": 0}
    streams = []
    for stream in probe(media).get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type not in counters:
            continue
        type_index = counters[codec_type]
        counters[codec_type] += 1
        tags = stream.get("tags") or {}
        language, region = split_tag(tags.get("language") or "")
        streams.append({"codec_type": codec_type, "type_index": type_index, "codec": stream.get("codec_name") or "unknown", "language": language, "region": region, "title": tags.get("title") or "", "default": bool((stream.get("disposition") or {}).get("default")), "forced": bool((stream.get("disposition") or {}).get("forced")), "external": False})
    return {"path": str(media), "streams": streams, "external_subtitles": external_subtitles(media)}


def checked_external(media: Path, value: str) -> Path:
    path = authorized_file(value)
    if path.parent != media.parent or path.suffix.lower() not in SUBTITLE_EXTENSIONS:
        raise HTTPException(400, "External subtitle is not associated with this media file")
    valid = {item["path"] for item in external_subtitles(media)}
    if str(path) not in valid:
        raise HTTPException(400, "External subtitle name does not match this media file")
    return path


@app.post("/api/media/edit-single")
def edit_single(request: SingleEditRequest) -> dict:
    source = authorized_file(request.path)
    original = source.stat()
    source_probe = probe(source)
    counts = {kind: sum(1 for stream in source_probe.get("streams", []) if stream.get("codec_type") == kind) for kind in TYPE_SPECIFIER}
    external = [(item, checked_external(source, item.path)) for item in request.external_subtitles if item.embed]
    temporary = source.with_name(f".{source.stem}.{uuid.uuid4().hex}.vse{source.suffix}")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source)]
    for _, subtitle in external:
        command += ["-i", str(subtitle)]
    command += ["-map", "0"]
    for input_index in range(1, len(external) + 1):
        command += ["-map", f"{input_index}:0"]
    command += ["-map_metadata", "0", "-c", "copy"]
    for update in request.tracks:
        if update.type_index >= counts[update.codec_type]:
            continue
        selector = f'{TYPE_SPECIFIER[update.codec_type]}:{update.type_index}'
        if update.language is not None or update.region is not None:
            command += [f"-metadata:s:{selector}", f"language={make_language(update.language, update.region)}"]
        if update.title is not None:
            command += [f"-metadata:s:{selector}", f"title={update.title.strip()}"]
    for index in range(counts["audio"]):
        command += [f"-disposition:a:{index}", "+forced" if request.forced_audio == index else "-forced"]
    for index in range(counts["subtitle"]):
        command += [f"-disposition:s:{index}", "+forced" if request.forced_subtitle == index else "-forced"]
    for external_index, (item, _) in enumerate(external):
        output_index = counts["subtitle"] + external_index
        command += [f"-metadata:s:s:{output_index}", f"language={make_language(item.language, item.region)}", f"-metadata:s:s:{output_index}", f"title={item.title.strip()}", f"-disposition:s:{output_index}", "+forced" if item.forced else "-forced"]
    command.append(str(temporary))
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=3600, check=True)
        os.chmod(temporary, original.st_mode)
        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(temporary, source)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        temporary.unlink(missing_ok=True)
        message = getattr(exc, "stderr", None) or "Media edit failed"
        raise HTTPException(422, message[-2000:]) from exc
    warnings = []
    for _, subtitle in external:
        try:
            subtitle.unlink()
        except OSError as exc:
            warnings.append(f"Embedded but could not remove {subtitle.name}: {exc}")
    return {"edited": str(source), "embedded": [str(path) for _, path in external], "warnings": warnings}
