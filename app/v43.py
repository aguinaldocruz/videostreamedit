from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from fastapi import HTTPException

import app.v28 as movie_import
import app.v7 as media_editor
from app.v2 import make_language
from app.v40 import app, record_track_name_corrections


logger = logging.getLogger("videostreamedit")
_remux_edit = media_editor.reorder_edit
MATROSKA_EXTENSIONS = {".mkv", ".mka", ".mks", ".mk3d"}


def typed_streams(data: dict) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {"audio": [], "subtitle": []}
    for stream in data.get("streams", []):
        if stream.get("codec_type") in result:
            result[stream["codec_type"]].append(stream)
    return result


def old_track_names(typed: dict[str, list[dict]]) -> dict[tuple[str, int], str]:
    return {
        (stream_type, index): str((stream.get("tags") or {}).get("title") or "").strip()
        for stream_type, streams in typed.items()
        for index, stream in enumerate(streams)
    }


def requires_container_rewrite(request: media_editor.ReorderEditRequest, typed: dict[str, list[dict]]) -> tuple[bool, list[Path]]:
    removed = set(request.remove)
    external_removed = [
        media_editor.checked_external(Path(request.path), item.path)
        for item in request.external_subtitles
        if f"external:{item.path}" in removed
    ]
    if any(identifier.startswith("embedded:") for identifier in removed):
        return True, external_removed
    if any(item.embed and f"external:{item.path}" not in removed for item in request.external_subtitles):
        return True, external_removed
    for stream_type, streams in typed.items():
        remaining = [index for index in range(len(streams)) if f"embedded:{stream_type}:{index}" not in removed]
        requested: list[int] = []
        for item in request.order:
            if item.source != "embedded" or item.codec_type != stream_type or item.type_index not in remaining or item.type_index in requested:
                continue
            requested.append(item.type_index)
        requested.extend(index for index in remaining if index not in requested)
        if requested != remaining:
            return True, external_removed
    return False, external_removed


def desired_tag(request: media_editor.ReorderEditRequest, stream_type: str, tag: str) -> str | None:
    return getattr(request, f"{tag}_{stream_type}")


def matroska_metadata_command(source: Path, request: media_editor.ReorderEditRequest, typed: dict[str, list[dict]]) -> list[str]:
    command = ["mkvpropedit", str(source)]
    edits = 0
    for update in request.tracks:
        streams = typed[update.codec_type]
        if update.type_index >= len(streams):
            continue
        selector = f"track:{'a' if update.codec_type == 'audio' else 's'}{update.type_index + 1}"
        properties: list[str] = []
        if update.language is not None or update.region is not None:
            ietf = make_language(update.language, update.region)
            properties += ["--set", f"language={update.language.strip() if update.language else 'und'}"]
            properties += (["--set", f"language-ietf={ietf}"] if ietf else ["--delete", "language-ietf"])
        if update.title is not None:
            properties += ["--set", f"name={update.title.strip()}"]
        if properties:
            command += ["--edit", selector, *properties]
            edits += 1
    for stream_type, streams in typed.items():
        short = "a" if stream_type == "audio" else "s"
        for index, stream in enumerate(streams):
            identifier = f"embedded:{stream_type}:{index}"
            current = stream.get("disposition") or {}
            wanted_default = identifier == desired_tag(request, stream_type, "default")
            wanted_forced = identifier == desired_tag(request, stream_type, "forced")
            properties = []
            if bool(current.get("default")) != wanted_default:
                properties += ["--set", f"flag-default={int(wanted_default)}"]
            if bool(current.get("forced")) != wanted_forced:
                properties += ["--set", f"flag-forced={int(wanted_forced)}"]
            if properties:
                command += ["--edit", f"track:{short}{index + 1}", *properties]
                edits += 1
    return command if edits else []


def apply_in_place(source: Path, request: media_editor.ReorderEditRequest, typed: dict[str, list[dict]], external_removed: list[Path]) -> dict:
    original = source.stat()
    command = matroska_metadata_command(source, request, typed)
    warnings = []
    if command:
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=600, check=True)
        except FileNotFoundError as exc:
            raise HTTPException(503, "mkvpropedit is not installed") from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(422, (getattr(exc, "stderr", None) or "Matroska metadata edit failed")[-2000:]) from exc
        try:
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        except OSError as exc:
            warnings.append(f"Metadata updated, but the original file timestamp could not be restored: {exc}")
            logger.warning("change=timestamp_restore_skipped file=%s error=%s", str(source).replace("\n", "\\n"), exc)
    for subtitle in external_removed:
        try:
            subtitle.unlink()
        except OSError as exc:
            warnings.append(f"Could not remove {subtitle.name}: {exc}")
    media = str(source).replace("\n", "\\n")
    for track in request.tracks:
        changed = []
        if track.language is not None or track.region is not None:
            changed.append("language=%s region=%s" % (track.language or "", track.region or ""))
        if track.title is not None:
            changed.append(f"title={track.title}")
        if changed:
            logger.info("change=track_metadata_in_place file=%s track=%s:%d %s", media, track.codec_type, track.type_index, " ".join(changed))
    logger.info("change=default_forced_in_place file=%s default_audio=%s forced_audio=%s default_subtitle=%s forced_subtitle=%s", media, request.default_audio or "none", request.forced_audio or "none", request.default_subtitle or "none", request.forced_subtitle or "none")
    for subtitle in external_removed:
        logger.info("change=external_subtitle_removed file=%s subtitle=%s", media, str(subtitle).replace("\n", "\\n"))
    logger.info(
        "change=media_edit_planned file=%s operation=in_place_mkvpropedit metadata=%s external_removed=%d",
        media, str(bool(command)).lower(), len(external_removed),
    )
    return {"edited": str(source), "warnings": warnings, "operation": "in_place_mkvpropedit"}


@app.post("/api/v43/media/edit")
def optimized_media_edit(request: media_editor.ReorderEditRequest) -> dict:
    source = media_editor.authorized_file(request.path)
    data = media_editor.probe(source)
    typed = typed_streams(data)
    before = old_track_names(typed)
    structural, external_removed = requires_container_rewrite(request, typed)
    if source.suffix.lower() in MATROSKA_EXTENSIONS and not structural:
        result = apply_in_place(source, request, typed, external_removed)
    else:
        logger.info(
            "change=media_edit_planned file=%s operation=single_remux reason=%s",
            str(source).replace("\n", "\\n"), "container_structure" if structural else "non_matroska",
        )
        result = _remux_edit(request)
        result["operation"] = "single_remux"
    record_track_name_corrections(request, before)
    return result


movie_import.reorder_edit = optimized_media_edit
