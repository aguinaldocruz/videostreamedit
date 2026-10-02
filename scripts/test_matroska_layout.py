"""Header-only Matroska layout checks without production DB or media writes."""

import ast
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


source = Path(__file__).resolve().parents[1] / "app/matroska_layout.py"
tree = ast.parse(source.read_text())
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {
    "_vint", "_element", "inspect_layout", "checkpoint", "revalidate_warning_rows",
}]
database = sqlite3.connect(":memory:")
database.row_factory = sqlite3.Row
database.execute("CREATE TABLE matroska_layout_check(path TEXT PRIMARY KEY,size INTEGER,modified_ns INTEGER,status TEXT,detail TEXT,checked_at TEXT DEFAULT CURRENT_TIMESTAMP)")


@contextmanager
def connect():
    with database:
        yield database


scope = {"Path": Path, "connect": connect, "MATROSKA_SUFFIXES": {".mkv"},
         "logger": logging.getLogger(__name__),
         "EBML_HEADER": 0x1A45DFA3, "SEGMENT": 0x18538067,
         "TRACKS": 0x1654AE6B, "CLUSTER": 0x1F43B675,
         "MAX_TOP_LEVEL_ELEMENTS": 4096, "MAX_HEADER_BYTES": 8 * 1024 * 1024}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), scope)
header = bytes.fromhex("1a45dfa3 80 18538067 ff")
tracks = bytes.fromhex("1654ae6b 80")
cluster = bytes.fromhex("1f43b675 80")
with TemporaryDirectory() as temp:
    good = Path(temp) / "good.mkv"
    late = Path(temp) / "late.mkv"
    malformed = Path(temp) / "broken.mkv"
    good.write_bytes(header + tracks + cluster)
    late.write_bytes(header + cluster + tracks)
    malformed.write_bytes(b"not an EBML header")
    assert scope["inspect_layout"](good)[0] == "ok"
    assert scope["inspect_layout"](late)[0] == "tracks_after_cluster"
    assert scope["inspect_layout"](malformed)[0] == "unknown"
    assert scope["checkpoint"](late) == "tracks_after_cluster"
    saved = database.execute("SELECT path,size,modified_ns,status FROM matroska_layout_check WHERE path=?", (str(late),)).fetchone()
    assert saved["status"] == "tracks_after_cluster"
    assert scope["revalidate_warning_rows"]([saved]) == {str(late)}
    late.write_bytes(header + tracks + cluster)
    assert scope["revalidate_warning_rows"]([saved]) == set()
    assert database.execute("SELECT status FROM matroska_layout_check WHERE path=?", (str(late),)).fetchone()["status"] == "ok"
print("PASS: bounded Matroska header-order check and changed-media checkpoint")
