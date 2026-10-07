"""Cache-first HTML cleanup and approved repairs with one verified container write.

Prepare every selected subtitle before committing anything. Texts are small;
the only media-sized temporary is a sibling output, atomically substituted for
the original after verification. No catalog scan or detector is started here.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from app.job_safety import output_space, replace_prepared, run_write_command, stamp
from app.subtitle_html import has_removable_html, strip_non_color_html
from app.subtitle_timing import container_start_ms, native_shift_ms, set_cached_timeline

logger = logging.getLogger("uvicorn.error")
TIMING = re.compile(r"^\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
                    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})(?:\s.*)?$")


def validate_srt(expected: str, actual: str) -> None:
    """Compare each cue's millisecond timing and text, not muxer numbering."""
    def cues(text):
        result = []
        for block in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip()):
            if not block.strip():
                continue
            lines = block.split("\n")
            position = 1 if lines[0].strip().isdecimal() else 0
            match = TIMING.fullmatch(lines[position]) if position < len(lines) else None
            if not match:
                raise RuntimeError("Malformed subtitle cue; original retained")
            values = tuple(int(value) for value in match.groups())
            times = tuple(((values[n] * 60 + values[n+1]) * 60 + values[n+2]) * 1000 + values[n+3] for n in (0, 4))
            result.append((times, tuple(line.strip() for line in lines[position + 1:])))
        return result
    before, after = cues(expected), cues(actual)
    if len(before) != len(after):
        raise RuntimeError(f"Subtitle output changed cue count ({len(before)} to {len(after)}); original retained")
    for number, (wanted, found) in enumerate(zip(before, after), 1):
        if wanted != found:
            reason = 'timing' if wanted[0] != found[0] else 'dialogue'
            raise RuntimeError(f"Subtitle output changed {reason} at cue {number}; original retained")


def _plan(media: Path, data: dict) -> dict:
    """Reuse the metadata-preserving writer without importing editor defaults."""
    from app.movie_import_pipeline import identify
    native = media.suffix.casefold() in {".mkv", ".mka", ".mks", ".mk3d", ".webm"}
    identified = identify(media) if native else None
    ordered = {kind: [] for kind in ("video", "audio", "subtitle")}
    entries, positions, global_entries = {}, {}, []
    native_types = {"video": "video", "audio": "audio", "subtitle": "subtitles"}
    if native and any(track["type"] not in native_types.values() for track in identified["tracks"]):
        raise HTTPException(422, "Unrecognized Matroska track type; cleanup refused without changing the original")
    for stream in data.get("streams", []):
        # FFprobe exposes Matroska cover art as an attached-picture video
        # stream. It is an attachment, not a TrackEntry; mkvmerge preserves
        # and the semantic verifier checks it separately.
        if native and (stream.get("disposition") or {}).get("attached_pic"):
            continue
        kind = stream.get("codec_type")
        if kind not in ordered:
            continue
        index = len(ordered[kind])
        track = None
        if native:
            matching = [track for track in identified["tracks"] if track["type"] == native_types[kind]]
            if index >= len(matching):
                raise HTTPException(422, "FFprobe and Matroska track identities disagree; original retained")
            track = matching[index]
        properties = copy.deepcopy(track["properties"]) if track else dict(
            language=(stream.get("tags") or {}).get("language") or "und",
            track_name=(stream.get("tags") or {}).get("title") or None,
            default_track=bool((stream.get("disposition") or {}).get("default")),
            forced_track=bool((stream.get("disposition") or {}).get("forced")),
        )
        key = f"embedded:{kind}:{index}"
        entry = dict(key=key, kind=kind, index=index, stream=stream, properties=properties, track=track,
                     input=0, track_id=track["id"] if track else stream["index"])
        ordered[kind].append(entry)
        entries[key] = entry
        global_entries.append(entry)
        if native:
            positions[key] = identified["tracks"].index(track)
    if native:
        if len(global_entries) != len(identified["tracks"]) or [positions[e["key"]] for e in global_entries] != list(range(len(global_entries))):
            raise HTTPException(422, "Ambiguous Matroska stream order; cleanup refused without changing the original")
    return dict(source=media, data=data, native=native, identified=identified, ordered=ordered,
                entries=entries, ordered_entries=global_entries, original_positions=positions, cache={},
                subtitle_validator=validate_srt, verify_passthrough_cache=True,
                source_shift_ms=native_shift_ms(identified) if native else 0)


def _ffmpeg_command(plan: dict, output: Path) -> list[str]:
    """Non-Matroska compatibility: copy everything except replaced text tracks."""
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(plan["source"])]
    replacements = {entry["stream"]["index"]: entry for entry in plan["ordered"]["subtitle"] if entry.get("path")}
    for number, entry in enumerate(replacements.values(), 1):
        entry["input"] = number
        command += ["-i", str(entry["path"])]
    for stream in plan["data"]["streams"]:
        replacement = replacements.get(stream["index"])
        command += ["-map", f"{replacement['input']}:0" if replacement else f"0:{stream['index']}"]
    command += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
    for number, stream in enumerate(plan["data"]["streams"]):
        flags = "+".join(key for key, value in (stream.get("disposition") or {}).items() if value) or "0"
        command += [f"-map_metadata:s:{number}", f"0:s:{stream['index']}", f"-disposition:{number}", flags]
        replacement = replacements.get(stream["index"])
        if replacement:
            codec = "mov_text" if output.suffix.casefold() in {".mp4", ".m4v", ".mov"} else "srt"
            command += [f"-c:s:{replacement['index']}", codec]
    if output.suffix.casefold() in {".mp4", ".m4v", ".mov"}:
        command += ["-movflags", "+faststart"]
    return command + [str(output)]


def _publish(media, data, cached, image_before, image_tracks, revision, expected_stamps) -> bool:
    """A cache outage after a verified commit never repeats that media write."""
    from app.subtitle_cache import complete_pending_media, enqueue_media, invalidate_and_enqueue_media_many, publish_replacement_cache
    from app.subtitle_cache_worker import image_track_manifest, text_track_manifest
    try:
        signature, tracks = text_track_manifest(media, data)
        after_image, after_images = image_track_manifest(media, data)
        if after_images != image_tracks or any(stamp(Path(path)) != before for path, before in expected_stamps.items()):
            raise RuntimeError("Media or external subtitles changed before cache publication")
        available = [replace(cached[key], codec=track["codec"]) for track in tracks
                     if (key := (track["source"], track["type_index"], track["external_path"])) in cached]
        images_complete = publish_replacement_cache(
            str(media), signature, {(track["source"], track["type_index"], track["external_path"]) for track in tracks}, available,
            image_before_signature=image_before, image_after_signature=after_image,
            expected_images={(track["type_index"], track["codec"]) for track in image_tracks},
        )
        if any(stamp(Path(path)) != before for path, before in expected_stamps.items()):
            raise RuntimeError("Source changed during cache publication")
        if len(available) == len(tracks) and images_complete:
            complete_pending_media(str(media), revision)
        else:
            enqueue_media(str(media))
        return True
    except Exception as exc:
        logger.warning("subtitle_html event=cache_publish_deferred path=%s error=%s", media, str(exc).replace("\n", " ")[:300])
        try:
            invalidate_and_enqueue_media_many([str(media)])
        except Exception:
            logger.exception("subtitle_html event=cache_recovery_deferred path=%s", media)
        return False


def clean_subtitles(media: Path, selections: list[dict], *, operation_id: str | None = None, progress=None) -> dict:
    return _rewrite_subtitles(media, selections, operation_id=operation_id, progress=progress)


def replace_reviewed_subtitles(media: Path, replacements: list[dict], *, operation_id: str | None = None, progress=None) -> dict:
    """Commit the exact approved texts, never reapply a possibly edited rule.

    Each reviewed input is bound to its original text digest. The caller also
    checks the media/sidecar configuration signature under its workflow lock.
    """
    approved = {(item['source'], item['type_index'], item['external_path']): item for item in replacements}
    if not approved or len(approved) != len(replacements):
        raise HTTPException(422, 'Choose distinct approved subtitle streams')
    return _rewrite_subtitles(media, replacements, operation_id=operation_id, progress=progress, approved=approved)


def _rewrite_subtitles(media: Path, selections: list[dict], *, operation_id: str | None = None, progress=None,
                       approved: dict | None = None) -> dict:
    from app.movie_import_pipeline import _native_command, _native_headers, _verify
    from app.subtitle_cache import TextSubtitle, get_valid_tracks, pending_revision, publish_track
    from app.subtitle_cache_worker import _extract_embedded_batch, _extract_track, image_track_manifest, text_track_manifest
    from app.subtitle_text_decode import decode_complete_srt
    from app.v2 import probe
    from app.v5 import checked_external, external_subtitles
    from app.v51 import decode_external

    started = time.monotonic()
    autofix = approved is not None
    encoding_only = autofix and any(item.get('normalize_encoding') for item in approved.values())
    action = 'UTF-8 encoding repair' if encoding_only else 'Autofix' if autofix else 'HTML cleanup'

    def transform(key, text, source_encoding=''):
        if not autofix:
            return strip_non_color_html(text) if has_removable_html(text) else text
        item = approved[key]
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != item['before_digest']:
            raise HTTPException(409, 'Subtitle text changed since approval; review a new preview. Originals retained.')
        fixed = item['text']
        if item.get('normalize_encoding'):
            from app.subtitle_encoding import validate_encoding_fix
            if source_encoding != item.get('source_encoding') or fixed != text:
                raise HTTPException(409, 'Encoding evidence or approved text changed; original retained')
            try:
                validate_encoding_fix(text, source_encoding)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        from app.subtitle_autofix_text import validate_replacement
        validate_replacement(text, fixed)
        return fixed
    tell = progress or (lambda _step, _message: None)
    tell(0, "Checking selected subtitles and loading their cache")
    original = stamp(media)
    data = probe(media)
    signature, tracks = text_track_manifest(media, data)
    image_before, images = image_track_manifest(media, data)
    sidecars = {str(Path(item["path"])): stamp(Path(item["path"])) for item in external_subtitles(media)}
    try:
        cached = {track.key: track for track in get_valid_tracks(str(media), signature)}
        revision = pending_revision(str(media))
    except Exception as exc:
        logger.warning("subtitle_html event=cache_read_unavailable path=%s error=%s", media, str(exc).replace("\n", " ")[:300])
        cached, revision = {}, None
    manifest = {(track["source"], track["type_index"], track["external_path"]): track for track in tracks}
    cached = {key: track for key, track in cached.items() if key in manifest and track.codec == manifest[key]["codec"]}
    wanted = {}
    for selection in selections:
        external = selection.get("external_path")
        if external:
            subtitle = checked_external(media, external)
            key = ("external", -1, str(subtitle))
        else:
            index = selection.get("type_index")
            key = ("embedded", index if index is not None else -1, "")
        if key not in manifest:
            raise HTTPException(422, f"{action} requires an existing text subtitle; image subtitles are not eligible")
        if autofix and manifest[key]['codec'] not in {'srt', 'subrip'}:
            raise HTTPException(422, 'Reviewed autofix currently supports SRT/SubRip only; no format conversion was performed')
        wanted[key] = manifest[key]
    legacy = {key for key in wanted if key in cached and cached[key].extraction_revision < 2}
    missing = [track for key, track in wanted.items() if (key not in cached or key in legacy) and track["source"] == "embedded"]
    cached_inputs = sum(key in cached and key not in legacy for key in wanted)
    tell(1, f"Preparing {len(wanted)} subtitle(s) · {cached_inputs} current cached inputs · {len(missing)} embedded extraction(s) needed")
    batch = _extract_embedded_batch(media, missing)
    for track in missing:
        item = batch.get(track["type_index"]) or _extract_track(media, track)
        cached[item.key] = item
        if item.key in legacy:
            if stamp(media) != original or text_track_manifest(media,data)[0] != signature:
                raise HTTPException(409,'Source changed while refreshing subtitle cache; originals retained')
            # No catalog-wide invalidation. A changed approved digest still
            # requires a renewed preview; never silently alter user consent.
            publish_track(str(media),signature,set(manifest),item)
    plan = None
    token = re.sub(r"[^A-Za-z0-9_-]", "", str(operation_id or uuid.uuid4().hex))[-48:] or uuid.uuid4().hex
    temporary = media.with_name(f".{media.stem}.subtitle-clean.vse-{token}{media.suffix}")
    # The same queued attempt owns this exact sibling name. An interrupted
    # uncommitted output is discarded before disk admission, not duplicated.
    temporary.unlink(missing_ok=True)
    committed_stamps = {str(media): original, **sidecars}
    external_prepared = []
    changed_embedded, changed_external = 0, 0
    remux_seconds, verification_seconds = 0.0, 0.0
    warnings = []
    try:
        with tempfile.TemporaryDirectory(prefix="vse-subtitle-cleanup-") as directory:
            folder = Path(directory)
            for key, track in wanted.items():
                if track["source"] == "embedded":
                    old = cached[key].text
                    cleaned = transform(key, old, cached[key].source_encoding)
                    if cleaned == old and not (autofix and approved[key].get('normalize_encoding')):
                        continue
                    if not autofix and has_removable_html(cleaned):
                        raise HTTPException(422, "Subtitle cleanup left unsupported markup; original retained")
                    validate_srt(cleaned, cleaned)
                    plan = plan or _plan(media, data)
                    entry = plan["ordered"]["subtitle"][track["type_index"]]
                    candidate = folder / f"cleaned-{track['type_index']}.srt"
                    candidate.write_text(cleaned, encoding="utf-8")
                    entry.update(path=candidate, cleaned=cleaned, verification_text=cleaned)
                    set_cached_timeline(entry, data, plan['native'], plan['identified'])
                    changed_embedded += 1
                else:
                    subtitle = Path(track["external_path"])
                    if subtitle.stat().st_size > 32 * 1024**2:
                        raise HTTPException(422, "External subtitle exceeds the 32 MiB safety limit")
                    raw = subtitle.read_bytes()
                    old, encoding = decode_external(raw)
                    if "\ufffd" in old:
                        raise HTTPException(422, "External subtitle cannot be safely decoded; original retained")
                    if subtitle.suffix.casefold() == ".srt":
                        decoded = decode_complete_srt(raw)
                        old = cached[key].text if key in cached and key not in legacy else decoded.text
                    cleaned = transform(key, old, decoded.encoding if subtitle.suffix.casefold() == '.srt' else encoding)
                    if cleaned == old and not (autofix and approved[key].get('normalize_encoding')):
                        continue
                    if not autofix and has_removable_html(cleaned):
                        raise HTTPException(422, "External subtitle cleanup left unsupported markup; original retained")
                    codec = {"UTF-8": "utf-8", "UTF-8 BOM": "utf-8-sig", "UTF-16": "utf-16", "Windows-1252": "cp1252"}[encoding]
                    if autofix:
                        # Corrected Unicode may not fit the original legacy
                        # charset. The filename/metadata stay intact; SRT is
                        # safely written as UTF-8, with its original BOM kept.
                        codec = 'utf-8-sig' if encoding == 'UTF-8 BOM' else 'utf-8'
                        encoding = 'UTF-8 BOM' if codec == 'utf-8-sig' else 'UTF-8'
                    prepared = subtitle.with_name(f".{subtitle.name}.vse-{token}.tmp")
                    external_prepared.append((subtitle, prepared, sidecars[str(subtitle)]))
                    with output_space(subtitle.parent, len(raw) * 2 + len(cleaned.encode(codec, errors="strict"))):
                        prepared.write_bytes(cleaned.encode(codec, errors="strict"))
                    verified, _encoding = decode_external(prepared.read_bytes())
                    if verified != cleaned:
                        raise RuntimeError("External subtitle verification failed; original retained")
                    if subtitle.suffix.casefold() == ".srt":
                        validate_srt(cleaned, verified)
                        normalized = TextSubtitle("external", -1, str(subtitle), track["codec"], verified, source_encoding=encoding)
                    else:
                        normalized = replace(_extract_track(media, {**track, "external_path": str(prepared)}), external_path=str(subtitle))
                        if not autofix and has_removable_html(normalized.text):
                            raise HTTPException(422, "External subtitle rendering still contains intrusive markup; original retained")
                    cached[key] = normalized
                    changed_external += 1
            if stamp(media) != original or text_track_manifest(media, data)[0] != signature or any(stamp(Path(path)) != before for path, before in sidecars.items()):
                raise HTTPException(409, "Source changed while preparing subtitles; originals retained")
            tell(2, f"All subtitles prepared · {changed_embedded} embedded · {changed_external} external changes")
            if plan:
                # Verify unchanged cached text in the same output extraction
                # pass, so it can be retained under the new source signature.
                for entry in plan["ordered"]["subtitle"]:
                    key = ("embedded", entry["index"], "")
                    if not entry.get("verification_text") and key in cached and cached[key].text:
                        entry["verification_text"] = cached[key].text
                        entry['text_origin_ms'] = container_start_ms(data) + plan['source_shift_ms'] if plan['native'] else 0
                with output_space(media.parent, int(original["size"] * 1.1) + 64 * 1024**2):
                    tell(3, f"Remuxing once to replace {changed_embedded} subtitle(s) · original protected")
                    remux_start = time.monotonic()
                    command = _native_command(plan, temporary, folder) if plan["native"] else _ffmpeg_command(plan, temporary)
                    written = run_write_command(command, media.parent, timeout=6 * 60 * 60,
                                                accepted_returncodes=(0, 1) if plan["native"] else (0,))
                    if plan["native"]:
                        _native_headers(plan, temporary)
                    from app.matroska_remux import ensure_front_track_headers
                    ensure_front_track_headers(temporary)
                    remux_seconds = time.monotonic() - remux_start
                    tell(4, "Verifying subtitle text, timing, stream order and metadata")
                    verification_start = time.monotonic()
                    _verify(plan, temporary)
                    if plan["native"]:
                        from app.matroska_remux import _verify_remux_warning
                        note = _verify_remux_warning(written, media, temporary, timestamp_shift_ms=plan['source_shift_ms'], excluded_stream_indexes={
                            entry["stream"]["index"] for entry in plan["ordered"]["subtitle"] if entry.get("cleaned")})
                        if note:
                            warnings.append(note)
                    verification_seconds = time.monotonic() - verification_start
                    output_data = probe(temporary)
                    if len(output_data.get("streams", [])) != len(data.get("streams", [])):
                        raise RuntimeError(f"{action} changed the total stream count; original retained")
                    for index, text in plan["cache"].items():
                        key = ("embedded", index, "")
                        codec = [stream["codec_name"] for stream in output_data["streams"] if stream["codec_type"] == "subtitle"][index]
                        cached[key] = replace(cached[key], codec=codec, text=text,
                                              source_encoding="UTF-8" if plan["ordered"]["subtitle"][index].get("cleaned") else cached[key].source_encoding,
                                              extraction_revision=2)
                    for index in plan.get('discard_cached_subtitles', ()):
                        cached.pop(('embedded', index, ''), None)
                    if text_track_manifest(media, data)[0] != signature or any(stamp(Path(path)) != before for path, before in sidecars.items()):
                        raise HTTPException(409, "Source changed before commit; originals retained")
                    tell(5, "Verification passed · committing the prepared media")
                    os.chmod(temporary, media.stat().st_mode)
                    committed_stamps[str(media)] = replace_prepared(temporary, media, original)
                    data = output_data
            else:
                tell(4, "No container remux needed · verifying external subtitles")
            for subtitle, prepared, before in external_prepared:
                os.chmod(prepared, subtitle.stat().st_mode)
                committed_stamps[str(subtitle)] = replace_prepared(prepared, subtitle, before)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = str(getattr(exc, "stderr", None) or exc)
        raise HTTPException(422, f"Subtitle {action.lower()} failed; uncommitted originals retained: " + detail[-1400:]) from exc
    finally:
        temporary.unlink(missing_ok=True)
        for _subtitle, prepared, _before in external_prepared:
            prepared.unlink(missing_ok=True)
    changed = bool(changed_embedded or changed_external)
    tell(6, "Publishing verified subtitles together in the cache")
    cache_published = False
    if changed or missing:
        cache_published = _publish(media, data, cached, image_before, images, revision, committed_stamps)
        if plan:
            from app.matroska_layout import safe_checkpoint
            safe_checkpoint(media, force=True)
    result = dict(changed=changed, path=str(media), cleaned=len(wanted), changed_subtitles=changed_embedded + changed_external,
                  remuxes=int(bool(plan)), cached_inputs=cached_inputs, extracted_inputs=len(missing),
                  cache_published=cache_published, seconds=round(time.monotonic() - started, 3),
                  remux_seconds=round(remux_seconds, 3), verification_seconds=round(verification_seconds, 3), warnings=warnings)
    logger.info("subtitle_html event=batch_completed path=%s targets=%d changed=%d remuxes=%d cached=%d extracted=%d seconds=%.2f",
                media, len(wanted), result["changed_subtitles"], result["remuxes"], cached_inputs, len(missing), result["seconds"])
    return result
