"""Exercise the queued stream-copy handler on a disposable Matroska file."""

import ast
import json
import logging
import os
import shutil
import subprocess
import sys
import types
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory


def module(name, **members):
    value = types.ModuleType(name)
    value.__dict__.update(members)
    sys.modules[name] = value
    return value


sys.modules.setdefault("app", types.ModuleType("app"))
progress = []
refreshed = []


class Database:
    def execute(self, _statement, _params):
        return self

    def fetchone(self):
        return (1,)


@contextmanager
def connection():
    yield Database()


module("app.v11", connection=connection, plex_authorized_file=Path)
module("app.v65", update_progress=lambda *_args: progress.append(_args))
module("app.v86", assert_media_editable=lambda _path: None)
module("app.v68", register_internal_change_scope=lambda *_args: refreshed.append("plex"))
module("app.subtitle_cache", invalidate_and_enqueue_media_many=lambda _paths: refreshed.append("cache"))
module("app.v80", request_media_indexes=lambda *_args: refreshed.append("index"))


def stamp(path):
    stat = Path(path).stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino}


def replace_prepared(source, destination, expected):
    assert stamp(destination) == expected, "source changed during remux"
    os.replace(source, destination)


def run_write_command(command, _directory, timeout, accepted_returncodes=(0,)):
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode not in accepted_returncodes:
        raise subprocess.CalledProcessError(result.returncode, command, stderr=result.stderr)
    return {"returncode": result.returncode, "output": (result.stdout + result.stderr).decode("utf-8"), "truncated": False}


source = Path(__file__).resolve().parents[1] / "app/matroska_remux.py"
tree = ast.parse(source.read_text())
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in {"_streams", "_semantic_snapshot", "_needs_native_remux",
                               "_restore_language_properties",
                               "_ffmpeg_dispositions", "_subtitle_packets", "_verify_remux_warning",
                               "process_matroska_layout_remux"}]
constants = [node for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id in {"_TRACK_PROPERTIES", "_GENERATED_TAGS"}
                     for target in node.targets)]
scope = {"Path": Path, "json": json, "logging": logging, "logger": logging.getLogger(__name__),
         "os": os, "shutil": shutil, "subprocess": subprocess, "uuid": uuid, "ET": ET,
         "output_space": lambda *_args: nullcontext(), "replace_prepared": replace_prepared,
         "run_write_command": run_write_command, "stamp": stamp, "checkpoint": lambda _path, **_kw: "ok"}
exec(compile(ast.Module(body=constants+functions, type_ignores=[]), str(source), "exec"), scope)

with TemporaryDirectory() as directory:
    media = Path(directory) / "sample.mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=32x32:r=1", "-t", "1", "-c:v", "mpeg4", str(media)], check=True)
    original = stamp(media)
    original_semantics = scope["_semantic_snapshot"](media)
    assert not scope["_needs_native_remux"](original_semantics)
    scope["inspect_layout"] = lambda path: (("tracks_after_cluster", "") if Path(path) == media and stamp(media) == original
                                             else (("ok", "") if Path(path).suffix == ".mkv" else ("not_matroska", "")))
    result = scope["process_matroska_layout_remux"](42, {"path": str(media)})
    assert result["status"] == "remuxed" and result["layout"] == "ok"
    assert stamp(media) != original and refreshed == ["plex", "cache", "index"]
    assert scope["_semantic_snapshot"](media) == original_semantics
    assert len(progress) >= 5 and not list(Path(directory).glob(".*.vse-remux-*.mkv"))

    changed_metadata = Path(directory) / "changed-metadata.mkv"
    shutil.copyfile(media, changed_metadata)
    subprocess.run(["mkvpropedit", str(changed_metadata), "--edit", "track:v1",
                    "--set", "name=Different video title"], check=True, capture_output=True)
    assert scope["_semantic_snapshot"](changed_metadata) != original_semantics, \
        "A changed track title must be detected before replacement"

    regional = Path(directory) / "regional.mkv"
    shutil.copyfile(media, regional)
    subprocess.run(["mkvpropedit", str(regional), "--edit", "track:v1",
                    "--set", "language=por", "--set", "language-ietf=pt-BR"],
                   check=True, capture_output=True)
    regional_semantics = scope["_semantic_snapshot"](regional)
    assert scope["_needs_native_remux"](regional_semantics)
    regional_stamp = stamp(regional)
    scope["inspect_layout"] = lambda path: (("tracks_after_cluster", "") if Path(path) == regional
                                             and stamp(regional) == regional_stamp else ("ok", ""))
    regional_result = scope["process_matroska_layout_remux"](43, {"path": str(regional)})
    assert regional_result["status"] == "remuxed"
    assert scope["_semantic_snapshot"](regional) == regional_semantics

    mixed = Path(directory) / "mixed-language.mkv"
    shutil.copyfile(media, mixed)
    subprocess.run(["mkvpropedit", str(mixed), "--edit", "track:v1",
                    "--set", "language-ietf=pt-BR"], check=True, capture_output=True)
    mixed_semantics = scope["_semantic_snapshot"](mixed)
    mixed_stamp = stamp(mixed)
    scope["inspect_layout"] = lambda path: (("tracks_after_cluster", "") if Path(path) == mixed
                                             and stamp(mixed) == mixed_stamp else ("ok", ""))
    mixed_result = scope["process_matroska_layout_remux"](43, {"path": str(mixed)})
    assert mixed_result["status"] == "remuxed"
    assert scope["_semantic_snapshot"](mixed) == mixed_semantics

    def write_changed_metadata(command, directory, timeout, **kwargs):
        result = run_write_command(command, directory, timeout, **kwargs)
        output = command[command.index("-o") + 1] if command[0] == "mkvmerge" else command[-1]
        subprocess.run(["mkvpropedit", output, "--edit", "track:v1",
                        "--set", "name=Unexpected title"], check=True, capture_output=True)
        return result

    metadata_refused = Path(directory) / "metadata-refused.mkv"
    shutil.copyfile(media, metadata_refused)
    metadata_stamp = stamp(metadata_refused)
    scope["inspect_layout"] = lambda path: (("tracks_after_cluster", "") if Path(path) == metadata_refused
                                             else ("ok", ""))
    scope["run_write_command"] = write_changed_metadata
    try:
        scope["process_matroska_layout_remux"](43, {"path": str(metadata_refused)})
    except RuntimeError as exc:
        assert "changed track properties" in str(exc)
    else:
        raise AssertionError("Changed track metadata was allowed to replace the original")
    assert stamp(metadata_refused) == metadata_stamp
    assert not list(Path(directory).glob(".*.vse-remux-*.mkv"))
    scope["run_write_command"] = run_write_command

    refused = Path(directory) / "refused.mkv"
    shutil.copyfile(media, refused)
    unchanged = stamp(refused)
    scope["inspect_layout"] = lambda _path: ("tracks_after_cluster", "")
    try:
        scope["process_matroska_layout_remux"](44, {"path": str(refused)})
    except RuntimeError as exc:
        assert "did not move track headers" in str(exc)
    else:
        raise AssertionError("Unverified output was allowed to replace the original")
    assert stamp(refused) == unchanged and not list(Path(directory).glob(".*.vse-remux-*.mkv"))

    subtitle = Path(directory) / "legacy.srt"
    subtitle.write_bytes("1\n00:00:00,000 --> 00:00:01,000\nNão é português errado.\n".encode("cp1252"))
    flagged = Path(directory) / "forced-not-default.mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(media), "-i", str(subtitle),
                    "-map", "0:v", "-map", "1:s", "-c", "copy", "-disposition:v", "0",
                    "-disposition:s", "forced", "-default_mode", "passthrough", str(flagged)], check=True)
    # No IETF field => FFmpeg path, with a forced but NOT default subtitle.
    subprocess.run(["mkvpropedit", str(flagged), "--edit", "track:v1", "--delete", "language-ietf",
                    "--edit", "track:s1", "--delete", "language-ietf", "--set", "flag-default=0"],
                   check=True, capture_output=True)
    semantics = scope["_semantic_snapshot"](flagged)
    assert dict(semantics["tracks"][1][2])["forced_track"] is True
    assert dict(semantics["tracks"][1][2])["default_track"] is False
    assert not scope["_needs_native_remux"](semantics)
    scope["inspect_layout"] = lambda path: (("tracks_after_cluster", "") if Path(path) == flagged else ("ok", ""))
    assert scope["process_matroska_layout_remux"](45, {"path": str(flagged)})["status"] == "remuxed"
    assert scope["_semantic_snapshot"](flagged) == semantics
    # Native path returns code 1 for existing invalid source UTF-8. Accept it
    # only with byte/timing equality plus all the normal semantic checks.
    subprocess.run(["mkvpropedit", str(flagged), "--edit", "track:s1", "--set", "language-ietf=pt-BR"],
                   check=True, capture_output=True)
    semantics = scope["_semantic_snapshot"](flagged)
    before_packets = scope["_subtitle_packets"](flagged)
    result = scope["process_matroska_layout_remux"](46, {"path": str(flagged)})
    assert scope["_semantic_snapshot"](flagged) == semantics
    assert scope["_subtitle_packets"](flagged) == before_packets
    warning = {"returncode": 1, "truncated": False,
               "output": "Warning: track 1: This text subtitle track contains invalid 8-bit characters outside valid multi-byte UTF-8 sequences."}
    assert "not repaired" in scope["_verify_remux_warning"](warning, flagged, flagged)
    original = stamp(flagged)

    def unknown_warning(command, directory, timeout, **kwargs):
        run_write_command(command, directory, timeout, **kwargs)
        return {"returncode": 1, "output": "Warning: unexplained source data loss", "truncated": False}

    scope["run_write_command"] = unknown_warning
    try:
        scope["process_matroska_layout_remux"](47, {"path": str(flagged)})
    except RuntimeError as exc:
        assert "manual review" in str(exc)
    else:
        raise AssertionError("Unknown warning was allowed to commit")
    assert stamp(flagged) == original
    assert not list(Path(directory).glob(".*.vse-remux-*.mkv"))

print("PASS: stream-copy repair validates and atomically replaces a disposable Matroska file")
