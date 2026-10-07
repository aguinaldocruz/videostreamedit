"""Real-tool regression of header relocation; no application DB/catalog writes."""
import ast
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import types
import uuid
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(file, scope):
    tree = ast.parse((ROOT / file).read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    # Imports are supplied explicitly: never initialize the production app.
    nodes = [n for n in nodes if not isinstance(n, ast.Assign) or all(
        not isinstance(t, ast.Name) or t.id != 'logger' for t in n.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, 'exec'), scope)

def stamp(path):
    s = Path(path).stat()
    return s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino

def replace(source, target, before):
    assert stamp(target) == before
    os.replace(source, target)

commands = []
def run(command, directory=None, timeout=60, accepted_returncodes=(0,)):
    commands.append(command)
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode not in accepted_returncodes:
        raise RuntimeError(result.stderr.decode(errors='replace'))
    return dict(returncode=result.returncode, output=(result.stdout + result.stderr).decode(), truncated=False)

scope = dict(Path=Path, json=json, logging=logging, logger=logging.getLogger(__name__),
             os=os, shutil=shutil, subprocess=subprocess, uuid=uuid, ET=ET,
             stamp=stamp, replace_prepared=replace, output_space=lambda *_a: nullcontext(),
             run_write_command=run)
load('app/matroska_layout.py', scope)
layout = types.ModuleType('app.matroska_layout')
layout.MATROSKA_SUFFIXES = scope['MATROSKA_SUFFIXES']
checkpoints = []
layout.safe_checkpoint = lambda p, **kw: checkpoints.append((p, kw))
sys.modules.setdefault('app', types.ModuleType('app'))
sys.modules['app.matroska_layout'] = layout
load('app/matroska_remux.py', scope)

with tempfile.TemporaryDirectory(prefix='vse-layout-test-') as folder:
    media = Path(folder) / 'test.mkv'
    run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=32x32:rate=1',
         '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-t', '1',
         '-c:v', 'mpeg4', '-c:a', 'aac', str(media)])
    original = stamp(media)
    count = len(commands)
    assert scope['ensure_front_track_headers'](media) is False
    assert stamp(media) == original and len(commands) == count
    run(['mkvpropedit', str(media), '--edit', 'track:a1', '--set', 'language=por',
         '--set', 'language-ietf=pt-BR'])
    assert scope['inspect_layout'](media)[0] == 'tracks_after_cluster'
    semantics = scope['_semantic_snapshot'](media)
    assert scope['ensure_front_track_headers'](media, live=True)
    assert scope['inspect_layout'](media)[0] == 'ok'
    assert scope['_semantic_snapshot'](media) == semantics
    assert checkpoints[-1][1] == {'force': True}
    assert not list(Path(folder).glob('.*vse-layout*'))
    run(['mkvpropedit', str(media), '--edit', 'track:a1', '--set', 'name=' + 'X' * 20000])
    assert scope['inspect_layout'](media)[0] == 'tracks_after_cluster'
    unchanged = media.read_bytes()
    saved = scope['run_write_command']
    def fail(*_a, **_kw):
        raise RuntimeError('Injected disk/tool failure')
    scope['run_write_command'] = fail
    try:
        scope['ensure_front_track_headers'](media)
        raise AssertionError('Failure was ignored')
    except RuntimeError as exc:
        assert 'Injected' in str(exc)
    assert media.read_bytes() == unchanged
    assert not list(Path(folder).glob('.*vse-layout*'))
    scope['run_write_command'] = saved
    assert scope['ensure_front_track_headers'](media)
    assert scope['inspect_layout'](media)[0] == 'ok'
    malformed = Path(folder) / 'bad.mkv'
    malformed.write_bytes(b'not matroska')
    try:
        scope['ensure_front_track_headers'](malformed)
        raise AssertionError('Malformed output accepted')
    except RuntimeError as exc:
        assert 'Cannot verify' in str(exc)
    assert scope['ensure_front_track_headers'](Path(folder) / 'text.srt') is False
print('PASS: metadata relocation repaired; exact semantics, fast no-op, forced refresh, failure retention and cleanup')
