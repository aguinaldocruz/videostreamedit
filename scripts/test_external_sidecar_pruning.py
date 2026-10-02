"""Isolated sidecar reconciliation test; no catalog or media writes."""

import ast
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


with TemporaryDirectory() as temporary:
    folder = Path(temporary)
    media = folder / "Episode.mkv"
    media.touch()
    present = folder / "Episode.en.srt"
    present.touch()
    missing = folder / "Episode.pt.srt"
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript("""
        CREATE TABLE media_stream_index(path TEXT, source TEXT, external_path TEXT);
        CREATE TABLE external_subtitle_index(media_path TEXT, external_path TEXT);
    """)
    for sidecar in (present, missing):
        database.execute("INSERT INTO media_stream_index VALUES(?,?,?)", (str(media), "external", str(sidecar)))
        database.execute("INSERT INTO external_subtitle_index VALUES(?,?)", (str(media), str(sidecar)))
    database.commit()

    @contextmanager
    def connection():
        with database:
            yield database

    source = (Path(__file__).resolve().parents[1] / "app/v79.py").read_text()
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "prune_missing_external_sidecars")
    function.decorator_list = []
    scope = {"connection": connection, "Path": Path}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "sidecar-test", "exec"), scope)
    prune = scope[function.name]
    assert prune({str(media)}) == 1
    assert prune({str(media)}) == 0
    assert [row[0] for row in database.execute("SELECT external_path FROM media_stream_index")] == [str(present)]
    assert [row[0] for row in database.execute("SELECT external_path FROM external_subtitle_index")] == [str(present)]

assert "external.append(item)" not in (Path(__file__).resolve().parents[1] / "app/v13.py").read_text()
print("PASS: missing external subtitle retired; existing sidecar preserved; editor does not resurrect stale rows")
