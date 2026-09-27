"""Test exact video-only command scope without a database or real media writes."""
import ast
from pathlib import Path
from types import SimpleNamespace

source = Path(__file__).resolve().parents[1] / 'app/v43.py'
tree = ast.parse(source.read_text())
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {'desired_tag', 'matroska_metadata_command'}]
sample = {'streams': [
    {'codec_type': 'video', 'tags': {'title': 'Video one'}},
    {'codec_type': 'audio', 'tags': {'title': 'Keep audio'}},
    {'codec_type': 'video', 'tags': {'title': 'Video two'}},
    {'codec_type': 'subtitle', 'tags': {'title': 'Keep subtitle'}},
]}
namespace = {'Path': Path, 'media_editor': SimpleNamespace(ReorderEditRequest=object, probe=lambda _: sample)}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
request = SimpleNamespace(clear_video_titles=True, tracks=[], default_audio='__preserve__',
                          forced_audio='__preserve__', default_subtitle='__preserve__', forced_subtitle='__preserve__')
typed = {'audio': [{'disposition': {'default': 1, 'forced': 1}}], 'subtitle': [{'disposition': {'default': 1}}]}
command = namespace['matroska_metadata_command'](Path('/fixture/media.mkv'), request, typed)
assert command == ['mkvpropedit', '/fixture/media.mkv', '--edit', 'track:v1', '--delete', 'name', '--edit', 'track:v2', '--delete', 'name'], command
sample['streams'] = []
assert namespace['matroska_metadata_command'](Path('/fixture/media.mkv'), request, typed) == []
print('PASS: only video stream titles removed; audio/subtitle flags and names untouched; empty titles are no-op')
