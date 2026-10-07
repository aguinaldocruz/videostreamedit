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
for permitted in ('<i>Olá</i>', '<I>Olá</I>', '<em>Olá</em>', '<u>Olá</u>', '<U>Olá</U>', '<u/>',
                  '<i><u>Olá</u></i>', '<u><i>Olá</i></u>', '<i><font color="red">Olá</font></i>',
                  '<span style="color: red"><i>Olá</i></span>', '<br>', '<br/>', '<br />', '<BR>', '</br>',
                  '<br>Olá</br>', '<i>Olá<br/>mundo</i>',
                  '<font color="red"><i><u>Olá<br>mundo</br></u></i></font>'):
    assert not has_removable_html(permitted), permitted
    assert classify(permitted) == 'None'
    assert not has_removable_html(strip_non_color_html(permitted))
assert strip_non_color_html('<i>Olá</i>') == '<i>Olá</i>'
assert strip_non_color_html('<em>Olá</em>') == '<i>Olá</i>'
assert strip_non_color_html('<u>Olá</u>') == '<u>Olá</u>'
assert strip_non_color_html('<U>Olá</U>') == '<u>Olá</u>'
assert has_removable_html('<u style="font-size: 40px">Olá</u>')
assert strip_non_color_html('<u style="font-size: 40px">Olá</u>') == '<u>Olá</u>'
assert has_removable_html('<u onclick="bad()">Olá</u>')
assert strip_non_color_html('<u onclick="bad()">Olá</u>') == '<u>Olá</u>'
assert has_removable_html('</u>Olá')
assert strip_non_color_html('Olá<br>mundo</br><br/><br />') == 'Olá<br>mundo</br><br/><br/>'
assert has_removable_html('<br style="font-size: 40px">')
assert strip_non_color_html('<br style="font-size: 40px">') == '<br>'
breaks = '1\n00:00:01,000 --> 00:00:02,000\n<font color="red"><i><u><b>Olá</b><br/>mundo</br></u></i></font>\n'
cleaned_breaks = strip_non_color_html(breaks)
assert cleaned_breaks == breaks.replace('<b>', '').replace('</b>', '')
assert classify(breaks) == 'HTML tags' and classify(cleaned_breaks) == 'None'
validate(cleaned_breaks, cleaned_breaks)
assert has_removable_html('<i style="font-size: 40px">Olá</i>')
assert strip_non_color_html('<i style="font-size: 40px">Olá</i>') == '<i>Olá</i>'
mixed = '1\n00:00:01,000 --> 00:00:02,000\n<i><font color="#00ff00" size="18">Olá</font></i>\n'
assert has_removable_html(mixed)
assert classify(mixed) == "HTML tags"
cleaned_mixed = strip_non_color_html(mixed)
assert '<font color="#00ff00">Olá</font>' in cleaned_mixed
assert cleaned_mixed == mixed.replace(' size="18"', '')
assert not has_removable_html(cleaned_mixed)
assert not has_removable_html('1\n00:00:01,000 --> 00:00:02,000\n<John> Hello\n')
assert '<John>' in strip_non_color_html('<John> Hello')
assert has_removable_html('<span style="color: red; font-size: 20px">Hello</span>')
assert strip_non_color_html('<span style="color: red; font-size: 20px">Hello</span>') == '<span style="color: red">Hello</span>'
expected = "1\n00:00:01,000 --> 00:00:02,000\nHello world\n\n2\n00:00:03,000 --> 00:00:04,000\nGoodbye\n"
validate(expected, expected.replace(",", "."))
for invalid in (expected.replace("Hello world", "Hello"), expected.replace("Goodbye", "<b>Goodbye</b>"),
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
    # Cleanup remuxes copy the prepared SRT packets; decoding/re-encoding can
    # normalize markup. Verify that breaks and every retained closing tag
    # survive the packet-copy path used to replace the selected subtitle.
    copied_breaks = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "srt", "-i", "pipe:0", "-c:s", "copy", "-f", "srt", "pipe:1"],
        input=cleaned_breaks.encode(), capture_output=True, check=True,
    ).stdout.decode("utf-8")
    validate(cleaned_breaks, copied_breaks)
print("PASS: color, italics, underline and line breaks excluded from findings and preserved, including closing tags; unsafe markup removed without losing cues or dialogue")
