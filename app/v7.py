from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.v2 import app, authorized_file, make_language, probe, tv_shows
from app.v5 import ExternalSubtitleChange, checked_external

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("uvicorn.error")


class TrackChange(BaseModel):
    codec_type: Literal["audio", "subtitle"]
    type_index: int = Field(ge=0)
    language: str | None = None
    region: str | None = None
    title: str | None = None


class OrderItem(BaseModel):
    source: Literal["embedded", "external"]
    codec_type: Literal["audio", "subtitle"]
    type_index: int | None = Field(default=None, ge=0)
    path: str | None = None


class ReorderEditRequest(BaseModel):
    path: str
    tracks: list[TrackChange] = []
    external_subtitles: list[ExternalSubtitleChange] = []
    order: list[OrderItem] = []
    default_audio: str | None = None
    forced_audio: str | None = None
    default_subtitle: str | None = None
    forced_subtitle: str | None = None
    remove: list[str] = []


def asset(content: str, media_type: str) -> Response:
    return Response(content, media_type=media_type, headers={"Cache-Control": "no-store, max-age=0"})


@app.middleware("http")
async def v7_assets(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text().replace('href="/app.css"', 'href="/assets/v7.css"').replace('src="/app.js"', 'src="/assets/v7.js"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v7.css":
        css = "\n".join((STATIC_DIR / name).read_text() for name in ("v3.css", "v4.css", "v5.css", "v7-addon.css"))
        css = css.replace("@import url('/base.css');", "").replace("@import url('/previous.css');", "")
        return asset(css, "text/css")
    if request.method == "GET" and request.url.path == "/assets/v7.js":
        javascript = (STATIC_DIR / "v5.js").read_text().replace("'/api/tv'", "'/api/v7/tv'")
        javascript += "\n" + (STATIC_DIR / "v7-addon.js").read_text()
        return asset(javascript, "text/javascript")
    return await call_next(request)


def episode_key(episode: dict) -> tuple:
    text = f'{episode.get("relative_path", "")} {episode.get("name", "")}'
    match = re.search(r"S(\d{1,3})E(\d{1,4})", text, re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2)), text.casefold()
    numbers = [int(value) for value in re.findall(r"\d+", text)]
    return (numbers[-2] if len(numbers) > 1 else 10**6, numbers[-1] if numbers else 10**6, text.casefold())


def season_key(season: dict) -> tuple:
    match = re.search(r"(\d+)", season["name"])
    return (0, int(match.group(1))) if match else (1, season["name"].casefold())


@app.get("/api/v7/tv")
def ordered_tv_shows() -> list[dict]:
    shows = tv_shows()
    for show in shows:
        show["seasons"].sort(key=season_key)
        for season in show["seasons"]:
            season["episodes"].sort(key=episode_key)
    return sorted(shows, key=lambda show: show["name"].casefold())


def disposition_flags(stream: dict, default: bool, forced: bool) -> str:
    dispositions = stream.get("disposition") or {}
    flags = [name for name, enabled in dispositions.items() if enabled and name not in {"default", "forced"}]
    if default:
        flags.append("default")
    if forced:
        flags.append("forced")
    return "+".join(flags) if flags else "0"


def key_for(item: OrderItem) -> str:
    return f"embedded:{item.codec_type}:{item.type_index}" if item.source == "embedded" else f"external:{item.path}"


def persist_remux_language_tags(
    media: Path,
    request: ReorderEditRequest,
    ordered: dict[str, list[tuple[str, int | str, dict | None]]],
    external_by_path: dict[str, tuple[ExternalSubtitleChange, Path]],
) -> None:
    """Write exact BCP-47 tags after FFmpeg remuxes a Matroska file."""
    if media.suffix.lower() not in {".mkv", ".mka", ".mks", ".mk3d"}:
        return
    requested = {(item.codec_type, item.type_index): item for item in request.tracks}
    command = ["mkvpropedit", str(media)]
    edits = 0
    for codec_type in ("audio", "subtitle"):
        short = "a" if codec_type == "audio" else "s"
        for output_index, (source_kind, identity, _) in enumerate(ordered[codec_type]):
            language = region = None
            if source_kind == "embedded":
                update = requested.get((codec_type, identity))
                if update is not None and (update.language is not None or update.region is not None):
                    language, region = update.language, update.region
            elif codec_type == "subtitle":
                external = external_by_path[str(identity)][0]
                language, region = external.language, external.region
            if language is None and region is None:
                continue
            ietf = make_language(language, region)
            command += ["--edit", f"track:{short}{output_index + 1}"]
            command += ["--set", f"language={language.strip() if language else 'und'}"]
            command += (["--set", f"language-ietf={ietf}"] if ietf else ["--delete", "language-ietf"])
            edits += 1
    if not edits:
        return
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=600, check=True)
    except FileNotFoundError as exc:
        raise HTTPException(503, "mkvpropedit is not installed") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(422, (getattr(exc, "stderr", None) or "Matroska language metadata edit failed")[-2000:]) from exc


@app.post("/api/v7/media/edit")
def reorder_edit(request: ReorderEditRequest) -> dict:
    source = authorized_file(request.path)
    original = source.stat()
    data = probe(source)
    typed = {"audio": [], "subtitle": []}
    for stream in data.get("streams", []):
        if stream.get("codec_type") in typed:
            typed[stream["codec_type"]].append(stream)
    removed = set(request.remove)
    external_remove = {
        item.path: checked_external(source, item.path)
        for item in request.external_subtitles
        if f"external:{item.path}" in removed
    }
    external_by_path = {
        item.path: (item, checked_external(source, item.path))
        for item in request.external_subtitles
        if item.embed and f"external:{item.path}" not in removed
    }
    ordered: dict[str, list[tuple[str, int | str, dict | None]]] = {"audio": [], "subtitle": []}
    seen = set()
    for item in request.order:
        identifier = key_for(item)
        if identifier in seen or identifier in removed:
            continue
        if item.source == "embedded" and item.type_index is not None and item.type_index < len(typed[item.codec_type]):
            ordered[item.codec_type].append(("embedded", item.type_index, typed[item.codec_type][item.type_index]))
            seen.add(identifier)
        elif item.source == "external" and item.codec_type == "subtitle" and item.path in external_by_path:
            ordered["subtitle"].append(("external", item.path, None))
            seen.add(identifier)
    for codec_type, streams in typed.items():
        for index, stream in enumerate(streams):
            identifier = f"embedded:{codec_type}:{index}"
            if identifier not in seen and identifier not in removed:
                ordered[codec_type].append(("embedded", index, stream))
    for path in external_by_path:
        identifier = f"external:{path}"
        if identifier not in seen and identifier not in removed:
            ordered["subtitle"].append(("external", path, None))
    external_inputs = list(external_by_path.items())
    input_index = {path: index + 1 for index, (path, _) in enumerate(external_inputs)}
    temporary = source.with_name(f".{source.stem}.{uuid.uuid4().hex}.vse{source.suffix}")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source)]
    for _, (_, path) in external_inputs:
        command += ["-i", str(path)]
    command += ["-map", "0:v?"]
    for codec_type in ("audio", "subtitle"):
        short = "a" if codec_type == "audio" else "s"
        for source_kind, identity, _ in ordered[codec_type]:
            command += ["-map", f"0:{short}:{identity}" if source_kind == "embedded" else f"{input_index[identity]}:0"]
    command += ["-map", "0:t?", "-map", "0:d?", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
    # Preserve effective Matroska metadata for every manually mapped stream.
    try:
        from app.v13 import matroska_tracks
        matroska = matroska_tracks(source)
    except Exception:
        matroska = {"audio": [], "subtitle": []}
    for codec_type in ("audio", "subtitle"):
        short = "a" if codec_type == "audio" else "s"
        for output_index, (source_kind, identity, stream) in enumerate(ordered[codec_type]):
            if source_kind != "embedded":
                continue
            properties = matroska[codec_type][identity] if identity < len(matroska[codec_type]) else {}
            tags = (stream or {}).get("tags") or {}
            language = properties.get("language_ietf") or properties.get("language") or tags.get("language") or ""
            title = properties.get("track_name") if "track_name" in properties else tags.get("title")
            if language:
                command += [f"-metadata:s:{short}:{output_index}", f"language={language}"]
            if title is not None:
                command += [f"-metadata:s:{short}:{output_index}", f"title={str(title).strip()}"]
    new_index = {(kind, identity): index for codec_type in ordered for index, (kind, identity, _) in enumerate(ordered[codec_type])}
    for update in request.tracks:
        output_index = new_index.get(("embedded", update.type_index)) if update.codec_type in ordered else None
        # The tuple key needs the media type because audio and subtitle indices overlap.
        for index, (kind, identity, _) in enumerate(ordered[update.codec_type]):
            if kind == "embedded" and identity == update.type_index:
                output_index = index
                break
        if output_index is None:
            continue
        short = "a" if update.codec_type == "audio" else "s"
        if update.language is not None or update.region is not None:
            command += [f"-metadata:s:{short}:{output_index}", f"language={make_language(update.language, update.region)}"]
        if update.title is not None:
            command += [f"-metadata:s:{short}:{output_index}", f"title={update.title.strip()}"]
    selections = {"audio": (request.default_audio, request.forced_audio), "subtitle": (request.default_subtitle, request.forced_subtitle)}
    for codec_type in ("audio", "subtitle"):
        short = "a" if codec_type == "audio" else "s"
        default_key, forced_key = selections[codec_type]
        for index, (kind, identity, stream) in enumerate(ordered[codec_type]):
            identifier = f"embedded:{codec_type}:{identity}" if kind == "embedded" else f"external:{identity}"
            command += [f"-disposition:{short}:{index}", disposition_flags(stream or {}, identifier == default_key, identifier == forced_key)]
            if kind == "external":
                item = external_by_path[identity][0]
                command += [f"-metadata:s:s:{index}", f"language={make_language(item.language, item.region)}", f"-metadata:s:s:{index}", f"title={item.title.strip()}"]
    command.append(str(temporary))
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=3600, check=True)
        persist_remux_language_tags(temporary, request, ordered, external_by_path)
        os.chmod(temporary, original.st_mode)
        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(temporary, source)
    except HTTPException:
        temporary.unlink(missing_ok=True)
        raise
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(422, (getattr(exc, "stderr", None) or "Media edit failed")[-2000:]) from exc
    warnings = []
    deleted_external = {path for _, path in external_by_path.values()} | set(external_remove.values())
    for subtitle in deleted_external:
        try:
            subtitle.unlink()
        except OSError as exc:
            warnings.append(f"Embedded but could not remove {subtitle.name}: {exc}")
    media = str(source).replace("\n", "\\n")
    for track in request.tracks:
        changed = []
        if track.language is not None or track.region is not None:
            changed.append("language=%s region=%s" % (track.language or "", track.region or ""))
        if track.title is not None:
            changed.append(f"title={track.title}")
        if changed:
            logger.info("change=track_metadata file=%s track=%s:%d %s", media, track.codec_type, track.type_index, " ".join(changed))
    logger.info("change=stream_order file=%s audio=%s subtitle=%s", media, ",".join(f"{kind}:{identity}" for kind, identity, _ in ordered["audio"]), ",".join(f"{kind}:{identity}" for kind, identity, _ in ordered["subtitle"]))
    logger.info("change=default_forced file=%s default_audio=%s forced_audio=%s default_subtitle=%s forced_subtitle=%s", media, request.default_audio or "none", request.forced_audio or "none", request.default_subtitle or "none", request.forced_subtitle or "none")
    for path in external_by_path:
        logger.info("change=external_subtitle_embedded file=%s subtitle=%s", media, path.replace("\n", "\\n"))
    for identifier in sorted(removed):
        logger.info("change=stream_removed file=%s stream=%s", media, identifier.replace("\n", "\\n"))
    logger.info("change=media_file_replaced file=%s", media)
    return {"edited": str(source), "warnings": warnings}
