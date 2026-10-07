from __future__ import annotations

import logging
import json
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
    default: bool | None = None
    forced: bool | None = None


class OrderItem(BaseModel):
    source: Literal["embedded", "external"]
    codec_type: Literal["audio", "subtitle"]
    type_index: int | None = Field(default=None, ge=0)
    path: str | None = None


class AudioCompatibility(BaseModel):
    stage_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    action: Literal['add', 'replace'] = 'add'


class ReorderEditRequest(BaseModel):
    path: str
    clear_video_titles: bool = False
    tracks: list[TrackChange] = []
    external_subtitles: list[ExternalSubtitleChange] = []
    order: list[OrderItem] = []
    default_audio: str | None = None
    forced_audio: str | None = None
    default_subtitle: str | None = None
    forced_subtitle: str | None = None
    final_version: bool | None = None
    remove: list[str] = []
    defer_language_detection: bool = False
    audio_compatibility: list[AudioCompatibility] = []
    subtitle_color: str | None = Field(default=None, pattern=r'^#[0-9a-fA-F]{6}$')


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
            elif source_kind == 'compatibility':
                from app.v13 import matroska_tracks
                originals = matroska_tracks(Path(request.path)).get('audio', [])
                properties = originals[int(identity)] if int(identity) < len(originals) else {}
                raw = str(properties.get('language_ietf') or properties.get('language') or 'und')
                parts = raw.split('-', 1)
                language, region = parts[0], parts[1] if len(parts) > 1 else ''
                update = requested.get(('audio', identity))
                if update is not None and (update.language is not None or update.region is not None):
                    language, region = update.language, update.region
            elif codec_type == "subtitle":
                external = external_by_path[str(identity)][0]
                # Unknown/missing filename tags must be imported as Plex's
                # undetermined language rather than an empty/invalid value.
                language, region = external.language or "und", external.region or ""
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


def _reorder_edit_impl(request: ReorderEditRequest) -> dict:
    from app.v86 import assert_media_editable
    assert_media_editable(request.path)
    source = authorized_file(request.path)
    original = source.stat()
    from app.job_safety import output_space, stamp, replace_prepared, run_write_command
    original_stamp = stamp(source)
    from app.review_audio import resolve_integrations
    audio_integrations = resolve_integrations(source, request.audio_compatibility)
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
    replacements = {}
    compatibility_inputs = {}
    for position, (change, audio_path, stage) in enumerate(audio_integrations):
        identity = stage['audio_index']
        if f'embedded:audio:{identity}' in removed or identity >= len(typed['audio']):
            raise HTTPException(409, 'Approved AAC source was removed in this draft; discard the audio approval before saving')
        input_number = len(external_inputs) + position + 1
        if change.action == 'replace':
            replacements[identity] = input_number
        else:
            ordered['audio'].append(('compatibility', identity, typed['audio'][identity]))
            compatibility_inputs[identity] = input_number
    input_index = {path: index + 1 for index, (path, _) in enumerate(external_inputs)}
    temporary = source.with_name(f".{source.stem}.{uuid.uuid4().hex}.vse{source.suffix}")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source)]
    for _, (_, path) in external_inputs:
        command += ["-i", str(path)]
    for _, audio_path, stage in audio_integrations:
        start_offset = float(json.loads(stage['metadata_json']).get('start_time') or 0)
        command += ['-itsoffset', str(start_offset), '-i', str(audio_path)]
    command += ["-map", "0:v?"]
    for codec_type in ("audio", "subtitle"):
        short = "a" if codec_type == "audio" else "s"
        for source_kind, identity, _ in ordered[codec_type]:
            if codec_type == 'audio' and (source_kind == 'compatibility' or identity in replacements):
                command += ['-map', f"{compatibility_inputs[identity] if source_kind == 'compatibility' else replacements[identity]}:a:0"]
            else:
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
            if source_kind not in ("embedded", "compatibility"):
                continue
            properties = matroska[codec_type][identity] if identity < len(matroska[codec_type]) else {}
            tags = (stream or {}).get("tags") or {}
            language = properties.get("language_ietf") or tags.get("language") or properties.get("language") or ""
            title = properties.get("track_name") if "track_name" in properties else tags.get("title")
            if language:
                command += [f"-metadata:s:{short}:{output_index}", f"language={language}"]
            if title is not None:
                command += [f"-metadata:s:{short}:{output_index}", f"title={str(title).strip()}"]
            if source_kind == 'compatibility':
                update = next((item for item in request.tracks if item.codec_type == 'audio' and item.type_index == identity), None)
                if update is not None and update.title is not None:
                    title = update.title
                command += [f'-metadata:s:a:{output_index}', f'title={str(title or "").strip()} · AAC stereo'.strip(' ·')]
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
            current = (stream or {}).get("disposition") or {}
            update = next((item for item in request.tracks if kind == 'embedded' and item.codec_type == codec_type and item.type_index == identity), None)
            flags = []
            for flag, selection in (("default", default_key), ("forced", forced_key)):
                override = getattr(update, flag, None)
                flags.append(override if override is not None else bool(current.get(flag)) if selection == '__preserve__' else identifier == selection)
            command += [f"-disposition:{short}:{index}", disposition_flags(stream or {}, *flags)]
            if kind == 'compatibility':
                command += [f'-disposition:a:{index}', '0']
            if kind == "external":
                item = external_by_path[identity][0]
                command += [f"-metadata:s:s:{index}", f"language={make_language(item.language, item.region)}", f"-metadata:s:s:{index}", f"title={item.title.strip()}"]
    command.append(str(temporary))
    # Admission covers the entire output write, not just a racy free-space check.
    disk_admission = output_space(source.parent, int(original.st_size * 1.1) + sum(audio.stat().st_size for _, audio, _ in audio_integrations) + 64 * 1024**2)
    disk_admission.__enter__()
    try:
        if audio_integrations:
            import shutil
            required = original.st_size + sum(audio.stat().st_size for _, audio, _ in audio_integrations) + 512 * 1024**2
            if shutil.disk_usage(source.parent).free < required:
                raise HTTPException(507, 'Insufficient space for the atomic container rewrite and safety reserve')
        if audio_integrations:
            import tempfile
            import time
            from app.review_audio import integration_building
            integration_building(request.audio_compatibility, temporary)
            process = None
            with tempfile.TemporaryFile(mode='w+t') as errors:
                try:
                    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)
                    started = time.monotonic()
                    while process.poll() is None:
                        if time.monotonic() - started > 3600 or shutil.disk_usage(source.parent).free < 512 * 1024**2:
                            raise HTTPException(507, 'Container rewrite stopped at the time/disk safety limit; original retained')
                        time.sleep(.5)
                    if process.returncode:
                        errors.seek(0)
                        raise HTTPException(422, errors.read()[-2000:] or 'AAC integration remux failed; original retained')
                finally:
                    if process and process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
        else:
            run_write_command(command, source.parent)
        persist_remux_language_tags(temporary, request, ordered, external_by_path)
        from app.matroska_remux import ensure_front_track_headers
        ensure_front_track_headers(temporary)
        if audio_integrations:
            output = probe(temporary)
            audio_streams = [s for s in output.get('streams', []) if s.get('codec_type') == 'audio']
            if len(audio_streams) != len(ordered['audio']):
                raise HTTPException(422, 'AAC integration failed stream-count verification; original retained')
            for index, (kind, identity, _) in enumerate(ordered['audio']):
                if kind == 'compatibility' or identity in replacements:
                    if audio_streams[index].get('codec_name') != 'aac' or audio_streams[index].get('channels') != 2:
                        raise HTTPException(422, 'AAC integration failed codec verification; original retained')
            from app.review_audio import fingerprint
            if any(fingerprint(source) != json.loads(stage['fingerprint_json']) for _, _, stage in audio_integrations):
                raise HTTPException(409, 'Media changed during AAC integration; original retained')
        os.chmod(temporary, original.st_mode)
        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
        if audio_integrations:
            from app.review_audio import integration_intent
            with temporary.open('rb') as prepared:
                os.fsync(prepared.fileno())
            integration_intent(request.audio_compatibility, temporary)
        replace_prepared(temporary, source, original_stamp)
        from app.matroska_layout import safe_checkpoint
        safe_checkpoint(source, force=True)
        if audio_integrations:
            from app.review_audio import integration_complete
            try:
                parent_fd = os.open(source.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
                integration_complete(request.audio_compatibility)
            except Exception:
                logger.exception('AAC integration committed; stage finalization will recover on startup')
    except HTTPException:
        temporary.unlink(missing_ok=True)
        if audio_integrations:
            from app.review_audio import integration_failed
            integration_failed(request.audio_compatibility)
        raise
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        temporary.unlink(missing_ok=True)
        if audio_integrations:
            from app.review_audio import integration_failed
            integration_failed(request.audio_compatibility)
        raise HTTPException(422, (getattr(exc, "stderr", None) or "Media edit failed")[-2000:]) from exc
    except OSError:
        temporary.unlink(missing_ok=True)
        if audio_integrations:
            from app.review_audio import integration_failed
            integration_failed(request.audio_compatibility)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        disk_admission.__exit__(None, None, None)
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
            changed.append(f"language={track.language or ''} region={track.region or ''}")
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


@app.post("/api/v7/media/edit")
def reorder_edit(request: ReorderEditRequest) -> dict:
    """Stable compatibility URL delegating to the canonical v43 editor.

    Older clients can keep their URL, but they now receive the same LUW,
    signature validation, targeted detection invalidation, index planning,
    final-version lock, and error semantics as current clients. The low-level
    remux helper above remains private because v43 uses it for its fallback.
    """
    from app.v43 import optimized_media_edit
    return optimized_media_edit(request)
