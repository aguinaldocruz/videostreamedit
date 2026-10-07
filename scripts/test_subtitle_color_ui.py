"""The color action must settle DOM observers and leave navigation responsive."""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True, executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())
    page.set_content('''<button id="navigate" onclick="this.textContent='Navigated'">Setup</button>
        <section id="setup"><div data-setup-panel="subtitle-color"></div></section>
        <dialog id="stream-dialog"><form id="stream-form">
        <div class="dialog-title"><div>Fixture media</div></div>
        <div id="stream-content"></div><div class="dialog-actions"><button data-close-stream>Close</button></div></form></dialog>''')
    page.evaluate('''() => {
        window.api=async()=>({color:'#FFFF00'});
        window.toast=()=>{};
        window.isStreamEditorBusy=()=>document.querySelector('#stream-dialog').dataset.editorPhase==='loading';
        // Break runaway microtasks so a regression fails instead of hanging CI.
        const NativeObserver=window.MutationObserver;
        window.observerCalls=0;
        window.MutationObserver=class extends NativeObserver {
            constructor(callback){super((records,observer)=>{
                if(++window.observerCalls>40){
                    observer.disconnect();throw Error('Observer did not settle');
                }
                callback(records,observer);
            });}
        };
    }''')
    page.add_script_tag(content=(ROOT/'app/static/v108-subtitle-color.js').read_text())
    page.wait_for_timeout(100)
    assert not errors, errors
    page.click('#navigate')
    assert page.locator('#navigate').inner_text() == 'Navigated'
    action = page.locator('[data-apply-subtitle-color]')
    assert action.is_hidden()
    page.evaluate('''() => {
        document.querySelector('#stream-content').innerHTML=
            '<div class="stream-row" data-codec-type="subtitle"></div>';
        document.querySelector('#stream-dialog').dataset.editorPhase='loading';
        document.querySelector('#stream-dialog').showModal();
    }''')
    page.wait_for_timeout(100)
    assert action.is_visible()
    assert action.is_disabled()
    page.evaluate("document.querySelector('#stream-dialog').dataset.editorPhase='ready'")
    page.wait_for_timeout(100)
    assert action.is_enabled()
    action.click()
    page.wait_for_function("document.querySelector('#stream-form').dataset.subtitleColorPending === '#FFFF00'")
    action.click()
    page.wait_for_function("!document.querySelector('#stream-form').dataset.subtitleColorPending")
    page.evaluate("document.querySelector('#stream-form').dataset.subtitleColorPending='#00FF00'")
    page.wait_for_timeout(100)
    assert action.inner_text() == 'Subtitle color · #00FF00'
    page.evaluate("document.querySelector('#stream-content').replaceChildren()")
    page.wait_for_timeout(100)
    assert action.is_hidden()
    page.evaluate("document.querySelector('#stream-dialog').close()")
    page.click('#navigate')
    assert not errors, errors
    assert page.evaluate('window.observerCalls') < 15
    browser.close()

print('PASS: color controls settle, navigation responds, and editor updates remain visible')
