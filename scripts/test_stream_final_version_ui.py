"""Final Version editor regression; every mutation uses an in-memory fixture.

By default the browser uses the working tree's real asset assembly. Set
VSE_TEST_DEPLOYED_ASSETS=1 to test the deployed bundle instead. No real media,
notes, jobs or final-version records are changed.
"""
import ast
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
MEDIA_PATH = '/fixture/final-version.mkv'


def working_tree_bundle():
    """Use the production assembly verbatim without importing the app/DB."""
    tree = ast.parse((ROOT / 'app/web_assets.py').read_text())
    owner = next(node for node in tree.body
                 if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == 'current_web_assets')
    branch = next(node for node in owner.body if isinstance(node, ast.If)
                  and any(isinstance(value, ast.Constant)
                          and value.value == '/assets/v19.js'
                          for value in ast.walk(node.test)))
    # The final return caches/serves the bundle; preceding statements only
    # read and concatenate the exact files used by the live web application.
    module = ast.Module(body=branch.body[:-1], type_ignores=[])
    scope = {'STATIC_DIR': ROOT / 'app/static'}
    exec(compile(ast.fix_missing_locations(module), 'web_assets.py', 'exec'), scope)
    return scope['javascript']


def main():
    finals = {MEDIA_PATH: False, '/fixture/next-final.mkv': True}
    writes, operations, errors = [], [], []
    fail_edit, fail_status = [False], [False]
    media = {'streams': [{'codec_type': 'subtitle', 'type_index': 0,
                          'codec': 'subrip', 'language': 'pt', 'region': 'BR',
                          'title': 'Original', 'external': False,
                          'default': True, 'forced': False}],
             'external_subtitles': [], 'portuguese_detection': []}

    with sync_playwright() as playwright:
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser = playwright.chromium.launch(headless=True, **(
            {'executable_path': executable} if executable else {}))
        page = browser.new_page(viewport={'width': 1366, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(request_route):
            request = request_route.request
            parsed = urlsplit(request.url)
            url = parsed.path
            if url.endswith('/media/details-fast'):
                return request_route.fulfill(json=media)
            if url.endswith('/final-version') and request.method == 'GET':
                if fail_status[0]:
                    fail_status[0] = False
                    return request_route.fulfill(status=503, json={
                        'detail': 'Fixture approval status unavailable'})
                path = parse_qs(parsed.query)['path'][0]
                return request_route.fulfill(json={
                    'path': path, 'final_version': finals.get(path, False),
                    'effective_final_version': finals.get(path, False)})
            if url.endswith('/change-requested'):
                return request_route.fulfill(json={'change_requested': False, 'requests': []})
            if url.endswith('/subtitle-properties'):
                return request_route.fulfill(json={'properties': []})
            if url.endswith('/subtitle-cache-status'):
                return request_route.fulfill(json={'tracks': []})
            if url.endswith('/projection'):
                return request_route.fulfill(json={'operations': operations})
            if url.endswith('/tv/edit-session/fixture') and request.method == 'GET':
                return request_route.fulfill(json={
                    'session_id': 'fixture', 'show_id': 999999, 'status': 'open',
                    'operations': operations})
            if request.method == 'GET':
                return request_route.continue_()  # Unrelated, read-only UI endpoints.
            body = request.post_data_json
            writes.append((url, body))
            if url.endswith('/media/edit'):
                if fail_edit[0]:
                    fail_edit[0] = False
                    return request_route.fulfill(status=409, json={
                        'detail': 'Fixture conflict; no change made'})
                if body.get('final_version') is not None:
                    finals[body['path']] = bool(body['final_version'])
                media['streams'][0].update(body['tracks'][0])
                return request_route.fulfill(json={'operation': 'metadata', 'warnings': []})
            if url.endswith('/final-version'):
                finals[body['path']] = bool(body['final_version'])
                return request_route.fulfill(json=body)
            if url.endswith('/operation'):
                operations.append(body['operation'])
                return request_route.fulfill(json={'ok': True})
            if url.endswith('/queue'):
                return request_route.fulfill(json={'id': 999999})
            if url.endswith('/video-titles'):
                return request_route.fulfill(json={'items': {}})
            return request_route.fulfill(json={'ok': True, 'needs_attention': False, 'reasons': []})

        page.route('**/api/**', route)
        if os.environ.get('VSE_TEST_DEPLOYED_ASSETS') != '1':
            bundle = working_tree_bundle()
            page.route('**/assets/v19.js', lambda request_route: request_route.fulfill(
                body=bundle, content_type='text/javascript'))
        page.goto(os.environ.get('VSE_TEST_URL', 'http://127.0.0.1:8383'),
                  wait_until='domcontentloaded')
        page.wait_for_function('typeof openEditor === "function" && !!window.updateStreamEditorContext')
        button = page.locator('#stream-final-version')

        def open_media(path=MEDIA_PATH):
            page.evaluate('(path)=>openEditor(path, "Final-version fixture")', path)
            ready()

        def ready(clean=True):
            page.wait_for_function('!window.isStreamEditorBusy?.()')
            page.wait_for_function('!document.querySelector("#global-busy-overlay")')
            if clean:
                assert page.evaluate('queuedChangeCount()') == 0

        def apply(mode='now', draft=False, clean=True):
            page.locator('#stream-form [type=submit]').click()
            if not draft:
                page.locator(f'[data-stream-apply-mode={mode}]').click()
            ready(clean=clean)

        def unfreezes():
            return [(url, body) for url, body in writes
                    if url.endswith('/final-version') and body.get('final_version') is False]

        open_media()
        assert button.inner_text() == 'Final version'
        # Approval is a proposal until Apply succeeds. Its authoritative
        # refresh must never issue an unfreeze request (the original bug).
        before = len(writes)
        button.click()
        assert button.inner_text() == 'Final version (pending)'
        assert not finals[MEDIA_PATH]
        assert not any(url.endswith(('/media/edit', '/final-version', '/queue', '/operation'))
                       for url, _ in writes[before:]), writes[before:]
        apply()
        assert finals[MEDIA_PATH], f'Apply immediately unfreezes its own approval: {writes[before:]}'
        assert button.inner_text() == 'Final version · set'
        assert button.get_attribute('aria-pressed') == 'true'
        assert page.locator('#stream-content [name=title]').is_disabled()
        assert not unfreezes()

        # Plain same-media rereads and Reset edits are internal refreshes,
        # not a new editing visit. Undoing the pending toggle is clean too.
        open_media()
        assert finals[MEDIA_PATH] and not unfreezes()
        button.click()
        assert button.inner_text() == 'Unfreeze final (pending)'
        assert not page.locator('#stream-content [name=title]').is_disabled()
        button.click()
        assert page.evaluate('queuedChangeCount()') == 0
        assert button.inner_text() == 'Final version · set'
        button.click()
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('.stream-reset-edits').click()
        ready()
        assert button.inner_text() == 'Final version · set'
        assert finals[MEDIA_PATH] and not unfreezes()

        # Explicit unfreezing uses Apply, then a failed approval keeps the
        # pending proposal; retry must commit it exactly once.
        button.click()
        apply()
        assert not finals[MEDIA_PATH] and button.inner_text() == 'Final version'
        button.click()
        fail_edit[0] = True
        apply(clean=False)
        assert not finals[MEDIA_PATH] and page.evaluate('queuedChangeCount()') > 0
        assert button.inner_text() == 'Final version (pending)'
        apply()
        assert finals[MEDIA_PATH] and button.inner_text() == 'Final version · set'

        # The established user rule still applies to an actual new visit:
        # close/reopen or moving to a different finalized media unfreezes it.
        page.locator('#stream-dialog [data-close-stream]').last.click()
        page.wait_for_function('!document.querySelector("#stream-dialog").open')
        open_media()
        assert not finals[MEDIA_PATH] and len(unfreezes()) == 1
        assert button.inner_text() == 'Final version'
        open_media('/fixture/next-final.mkv')
        assert not finals['/fixture/next-final.mkv'] and len(unfreezes()) == 2
        open_media()

        # Queueing does not falsely display the proposed approval as saved.
        button.click()
        apply(mode='queue')
        queued = [body for url, body in writes if url.endswith('/queue')][-1]
        assert queued['payload']['edit']['final_version'] is True
        assert not finals[MEDIA_PATH] and button.inner_text() == 'Final version'
        finals[MEDIA_PATH] = True  # Simulated queued task completion.
        open_media()
        assert button.inner_text() == 'Final version · set'
        assert len(unfreezes()) == 2

        # An unavailable read must not present an unchecked (false) approval.
        fail_status[0] = True
        open_media()
        assert button.inner_text() == 'Final version · unknown'
        assert button.is_disabled()
        assert page.locator('#stream-content [name=title]').is_disabled()
        open_media()
        button.click()
        apply()

        # Draft approval journals only; no real file, queue, or final-version
        # mutation is made, and the label distinguishes draft from persisted.
        page.evaluate('''() => {
          window.tvShowEditSession={session_id:"fixture",show_id:999999,status:"open"};
          state.currentShow={id:999999,seasons:[{episodes:[{path:"/fixture/final-version.mkv"}]}]};
          window.projectTvEpisodeEdit=()=>{};window.markTvEditDirty=()=>{};
        }''')
        open_media()
        before = len(writes)
        button.click()
        apply(draft=True)
        assert button.inner_text() == 'Final version · draft'
        assert button.get_attribute('aria-pressed') == 'true'
        assert not finals[MEDIA_PATH]
        assert operations[-1]['direct_edit']['final_version'] is True
        assert not any(url.endswith(('/queue', '/media/edit', '/final-version'))
                       for url, _ in writes[before:]), writes[before:]
        assert not page.locator('#stream-content [name=title]').is_disabled()
        open_media()
        assert button.inner_text() == 'Final version · draft'
        assert page.evaluate('queuedChangeCount()') == 0

        # Import edits change a destination copy. They must neither unfreeze
        # an approved source nor lock an unindexed source's editable rows.
        page.locator('#stream-dialog [data-close-stream]').last.click()
        page.wait_for_function('!document.querySelector("#stream-dialog").open')
        finals[MEDIA_PATH] = True
        page.evaluate('''() => {
          window.tvShowEditSession=null;
          movieImportMode={editing:true,source:"/fixture/final-version.mkv"};
        }''')
        before = len(unfreezes())
        open_media()
        assert finals[MEDIA_PATH] and len(unfreezes()) == before
        assert button.is_disabled() and 'imported' in button.get_attribute('title')
        assert not page.locator('#stream-content [name=title]').is_disabled()
        assert not errors, errors
        browser.close()
    print('PASS: Final Version apply/refresh/reset, failed retry, new-visit unfreeze, '
          'queue, unreadable status, TV-show draft and import; no real mutations sent')


if __name__ == '__main__':
    main()
