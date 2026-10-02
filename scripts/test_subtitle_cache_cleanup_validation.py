"""Reject destructive subtitle cleanup output that loses text or cue timing."""

import ast
import re
import shutil
import subprocess
from pathlib import Path
from app.subtitle_html import has_removable_html, strip_non_color_html


tree = ast.parse((Path(__file__).resolve().parents[1] / "app/v51.py").read_text())
names = {"markup_kind", "subtitle_payload_lines", "validate_cleaned_srt"}
nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
scope = {
    "has_removable_html": has_removable_html,
    "ASS_TAG": re.compile(r"\{\\[^}]+}"),
    "SRT_TIMING_LINE": re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->\s+\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}"),
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "subtitle-cleanup-test", "exec"), scope)
validate = scope["validate_cleaned_srt"]
classify = scope["markup_kind"]
color_only = '1\n00:00:01,000 --> 00:00:02,000\n<font color="#00ff00">Olá</font>\n'
assert not has_removable_html(color_only)
assert classify(color_only) == "None"
assert strip_non_color_html(color_only) == color_only
mixed = '1\n00:00:01,000 --> 00:00:02,000\n<i><font color="#00ff00" size="18">Olá</font></i>\n'
assert has_removable_html(mixed)
assert classify(mixed) == "HTML tags"
cleaned_mixed = strip_non_color_html(mixed)
assert '<font color="#00ff00">Olá</font>' in cleaned_mixed
assert not has_removable_html(cleaned_mixed)
assert not has_removable_html('1\n00:00:01,000 --> 00:00:02,000\n<John> Hello\n')
assert '<John>' in strip_non_color_html('<John> Hello')
assert has_removable_html('<span style="color: red; font-size: 20px">Hello</span>')
assert strip_non_color_html('<span style="color: red; font-size: 20px">Hello</span>') == '<span style="color: red">Hello</span>'
expected = "1\n00:00:01,000 --> 00:00:02,000\nHello world\n\n2\n00:00:03,000 --> 00:00:04,000\nGoodbye\n"
validate(expected, expected.replace(",", "."))
for invalid in (expected.replace("Hello world", "Hello"), expected.replace("Goodbye", "<i>Goodbye</i>"),
                expected.replace("2\n00:00:03,000 --> 00:00:04,000\nGoodbye", "")):
    try:
        validate(expected, invalid)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Unverified subtitle output was accepted")
numeric_dialogue = "1\n00:00:01,000 --> 00:00:02,000\n42\n"
try:
    validate(numeric_dialogue, numeric_dialogue.replace("42", "43"))
except RuntimeError:
    pass
else:
    raise AssertionError("Changed numeric dialogue was accepted")
if shutil.which("ffmpeg"):
    source = Path(__file__).resolve().parent / "fixtures/review.srt"
    cleaned = strip_non_color_html(source.read_text())
    converted = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "srt", "-i", "pipe:0", "-f", "srt", "pipe:1"],
        input=cleaned.encode(), capture_output=True, check=True,
    ).stdout.decode("utf-8")
    validate(cleaned, converted)
    converted_color = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "srt", "-i", "pipe:0", "-f", "srt", "pipe:1"],
        input=cleaned_mixed.encode(), capture_output=True, check=True,
    ).stdout.decode("utf-8")
    validate(cleaned_mixed, converted_color)
print("PASS: cleaned subtitle output retains cues and dialogue and has no remaining HTML tags")
