"""Browser regression checks. All mutation requests are intercepted, never sent."""
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        errors, writes, operations = [], [], []
        fail_next = [False]
        media = {"streams": [{"codec_type": "subtitle", "type_index": 0, "codec": "subrip",
                              "language": "pt", "region": "", "title": "Original",
                              "external": False, "default": True, "forced": False}],
                 "external_subtitles": [], "portuguese_detection": []}
        page.on("pageerror", lambda e: errors.append(str(e)))

        def route(r):
            url = urlsplit(r.request.url).path
            if url.endswith('/media/details-fast'):
                return r.fulfill(json=media)
            if url.endswith('/change-requested'):
                return r.fulfill(json={"change_requested": False, "requests": []})
            if url.endswith('/subtitle-properties'):
                return r.fulfill(json={"properties": []})
            if url.endswith('/projection'):
                return r.fulfill(json={"operations": operations})
            if url.endswith('/tv/edit-session/fixture') and r.request.method == 'GET':
                return r.fulfill(json={"session_id": "fixture", "show_id": 1,
                                       "status": "editing", "operations": operations})
            if r.request.method == 'GET':
                return r.continue_()
            writes.append(url)
            if url.endswith('/media/edit'):
                if fail_next[0]:
                    fail_next[0] = False
                    return r.fulfill(status=409, json={"detail": "Fixture conflict; no change made"})
                body = r.request.post_data_json
                media['streams'][0].update(body['tracks'][0])
                return r.fulfill(json={"warnings": [], "operation": "metadata"})
            if url.endswith('/operation'):
                operations.append(r.request.post_data_json['operation'])
                return r.fulfill(json={"ok": True})
            if url.endswith('/queue'):
                return r.fulfill(json={"id": 999999})
            if url.endswith('/video-titles'):
                return r.fulfill(json={"items": {}})
            return r.fulfill(json={"ok": True, "needs_attention": False, "reasons": []})

        page.route('**/api/**', route)
        page.goto('http://127.0.0.1:8383', wait_until='domcontentloaded')
        page.wait_for_function('typeof openEditor === "function" && !!window.updateStreamEditorContext')
        page.evaluate('openEditor("/fixture/test.mkv", "Editor test")')
        title = page.locator('#stream-content [name=title]')

        def change(value):
            page.wait_for_function('!window.isStreamEditorBusy?.()')
            page.wait_for_function('!document.querySelector("#global-busy-overlay")')
            title.fill(value)
            # Do not open the learned-value confirmation dialog in this test.
            title.evaluate('(e)=>delete e.dataset.dirty')
            assert page.evaluate('queuedChangeCount()') > 0, page.evaluate('({baseline:editorBaseline,current:editorSnapshot(),phase:document.querySelector("#stream-dialog").dataset.editorPhase})')

        def ready():
            try:
                page.wait_for_function('document.querySelector("#stream-dialog").dataset.editorPhase === "ready"')
            except Exception:
                print('Editor wait diagnostics:', page.evaluate('({phase:document.querySelector("#stream-dialog").dataset.editorPhase, modeDialog:document.querySelector("#stream-apply-mode-dialog")?.open, session:window.tvShowEditSession?.status})'), errors, writes)
                raise
            page.wait_for_function('!document.querySelector("#global-busy-overlay")')
            assert page.evaluate('queuedChangeCount()') == 0

        change('Applied')
        page.locator('#stream-form [type=submit]').click()
        page.locator('[data-stream-apply-mode=now]').click()
        ready()
        assert title.input_value() == 'Applied'
        # A failed apply keeps the proposal, allows retry, and never clears it.
        change('Retry proposal')
        fail_next[0] = True
        page.locator('#stream-form [type=submit]').click()
        page.locator('[data-stream-apply-mode=now]').click()
        page.wait_for_function('document.querySelector("#stream-dialog").dataset.editorPhase === "ready"')
        assert page.evaluate('queuedChangeCount()') > 0
        assert title.input_value() == 'Retry proposal'
        page.locator('#stream-form [type=submit]').click()
        page.locator('[data-stream-apply-mode=now]').click()
        ready()
        assert title.input_value() == 'Retry proposal'
        # Click-only edits after apply must not be hidden by an old clean latch.
        page.locator('#stream-final-version').click()
        assert page.evaluate('queuedChangeCount()') > 0
        page.locator('#stream-final-version').click()
        assert page.evaluate('queuedChangeCount()') == 0
        remove = page.locator('#stream-content [name=remove]').first
        remove.evaluate('(e)=>{e.checked=true;e.dispatchEvent(new Event("change",{bubbles:true}))}')
        assert page.evaluate('queuedChangeCount()') > 0
        remove.evaluate('(e)=>{e.checked=false;e.dispatchEvent(new Event("change",{bubbles:true}))}')
        change('Queued')
        page.locator('#stream-form [type=submit]').click()
        page.locator('[data-stream-apply-mode=queue]').click()
        ready()
        assert title.input_value() == 'Retry proposal'
        # Draft mode applies only to the journal, not to the media or queue.
        page.evaluate('''() => {
          window.tvShowEditSession={session_id:"fixture",show_id:1,status:"editing"};
          state.currentShow={id:1,seasons:[{episodes:[{path:"/fixture/test.mkv"}]}]};
          window.projectTvEpisodeEdit=()=>{};window.markTvEditDirty=()=>{};
        }''')
        page.evaluate('openEditor("/fixture/test.mkv", "Draft episode")')
        before = len(writes)
        change('Draft title')
        page.locator('#stream-form [type=submit]').click()
        ready()
        assert title.input_value() == 'Draft title'
        assert not page.locator('#stream-apply-mode-dialog').is_visible()
        assert not any(u.endswith('/queue') or u.endswith('/media/edit') for u in writes[before:])
        assert media['streams'][0]['title'] == 'Retry proposal'
        change('Unapplied')
        page.wait_for_function('!window.isStreamEditorBusy?.()')
        page.locator('#stream-dialog [data-close-stream]').last.click()
        page.locator('#pending-navigation-dialog').wait_for(state='visible')
        assert not page.locator('[data-queue-pending]').is_visible()
        page.locator('#apply-before-navigation').click()
        ready()
        assert page.evaluate('document.querySelector("#stream-form").dataset.applyModeConfirmed') is None
        # Filtered subtitle flag changes must project in draft mode, including
        # a language change in the same operation (use original target IDs).
        operations.clear()
        media['streams'][0]['forced'] = True
        operations.append({'target_keys':['embedded:subtitle:0'],
                           'filters':{'stream_type':'subtitle','language':'pt'},
                           'language':'en','region':'','track_name':'',
                           'forced_action':'clear','default_action':'clear'})
        # The preceding navigation closes the draft context. This independent
        # fixture must establish its own session instead of relying on a stale
        # global surviving production session polling/navigation.
        page.evaluate('''() => {
          window.tvShowEditSession={session_id:"fixture",show_id:1,status:"editing"};
          state.currentShow={id:1,seasons:[{episodes:[{path:"/fixture/test.mkv"}]}]};
        }''')
        page.evaluate('openEditor("/fixture/test.mkv", "Draft flags")')
        ready()
        assert not page.locator('#stream-content [name=forced-subtitle]').first.is_checked()
        assert not page.locator('#stream-content [name=default-subtitle]').first.is_checked()
        assert media['streams'][0]['forced'] is True
        page.set_viewport_size({"width": 800, "height": 700})
        assert page.locator('#stream-content').evaluate('(e)=>e.scrollWidth>e.clientWidth')
        page.screenshot(path='/tmp/stream-editor-tested.png')
        assert not errors, errors
        browser.close()
        print('PASS: direct apply, queue, draft apply/navigation, clean baseline, horizontal scrolling; no media writes sent')


if __name__ == '__main__':
    main()
