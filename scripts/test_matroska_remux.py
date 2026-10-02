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


def run_write_command(command, _directory, timeout):
    subprocess.run(command, check=True, capture_output=True, timeout=timeout)


source = Path(__file__).resolve().parents[1] / "app/matroska_remux.py"
tree = ast.parse(source.read_text())
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in {"_streams", "process_matroska_layout_remux"}]
scope = {"Path": Path, "json": json, "logging": logging, "logger": logging.getLogger(__name__),
         "os": os, "shutil": shutil, "subprocess": subprocess, "uuid": uuid,
         "output_space": lambda *_args: nullcontext(), "replace_prepared": replace_prepared,
         "run_write_command": run_write_command, "stamp": stamp, "checkpoint": lambda _path: "ok"}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), scope)

with TemporaryDirectory() as directory:
    media = Path(directory) / "sample.mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=32x32:r=1", "-t", "1", "-c:v", "mpeg4", str(media)], check=True)
    original = stamp(media)
    scope["inspect_layout"] = lambda path: (("tracks_after_cluster", "") if Path(path) == media and stamp(media) == original
                                             else (("ok", "") if Path(path).suffix == ".mkv" else ("not_matroska", "")))
    result = scope["process_matroska_layout_remux"](42, {"path": str(media)})
    assert result["status"] == "remuxed" and result["layout"] == "ok"
    assert stamp(media) != original and refreshed == ["plex", "cache", "index"]
    assert len(progress) >= 5 and not list(Path(directory).glob(".*.vse-remux-*.mkv"))

    refused = Path(directory) / "refused.mkv"
    shutil.copyfile(media, refused)
    unchanged = stamp(refused)
    scope["inspect_layout"] = lambda _path: ("tracks_after_cluster", "")
    try:
        scope["process_matroska_layout_remux"](43, {"path": str(refused)})
    except RuntimeError as exc:
        assert "did not move track headers" in str(exc)
    else:
        raise AssertionError("Unverified output was allowed to replace the original")
    assert stamp(refused) == unchanged and not list(Path(directory).glob(".*.vse-remux-*.mkv"))

print("PASS: stream-copy repair validates and atomically replaces a disposable Matroska file")
