"""Compare browser cleanup offers to the server classifier with the same text."""
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.subtitle_html import has_removable_html

cases=['<i>Olá</i>','<I>Olá</I>','<em>Hello</em>','<font color="red"><i>Hello</i></font>',
       '<span style="color: #fff">Hello</span>','<i style="font-size: 22px">Hello</i>',
       '<font color="red" size="22">Hello</font>','<span style="color: red; font-size: 20px">Hello</span>',
       '<b><i>Hello</i></b>','<u>Hello</u>','<U>Hello</U>','<i><u>Hello</u></i>',
       '<u><font color="red">Hello<br>world</br></font></u>','<u/>',
       '<u style="font-size: 22px">Hello</u>','<u onclick="bad()">Hello</u>','</u>Hello',
       '<b><u>Hello</u></b>','<John> Hello','</i>Hello', '<br/>Hello',
       '<span style="color: rgba(1,2,3,0.5)"><em>Hello</em></span>',
       '<font color="red" onclick="bad()">Hello</font>','<i/>Hello','<font color="red"></font>',
       '<i><b>Hello</i></b>','Hello','1\n00:00:00,000 --> 00:00:01,000\n<i>Hello</i>',
       '<br>','<br/>','<br />','<BR>','</br>','</ BR >','<br>Hello</br>',
       '<i><font color="red">Hello<br/>world</br></font></i>',
       '<span style="color: #fff">Hello<br>world</span>',
       '<b>Hello<br>world</b>', '<br style="font-size: 20px">', '<br onclick="bad()"/>']
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page=browser.new_page();page.set_content('<div></div>')
    page.add_script_tag(content=(ROOT/'app/static/subtitle-html.js').read_text())
    detected=page.evaluate('cases=>cases.map(window.hasRemovableSubtitleHtml)',cases)
    assert detected==[has_removable_html(text) for text in cases],list(zip(cases,detected))
    assert not detected[cases.index('<u>Hello</u>')]
    assert not detected[cases.index('<i><u>Hello</u></i>')]
    assert detected[cases.index('<b><u>Hello</u></b>')]
    browser.close()
print('PASS: browser/server agree on color/italic/underline/break whitelist including closing tags, unsafe attributes, mixed tags and malformed markup')
