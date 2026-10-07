"""Damage-report browser regression, with report fixtures and all writes blocked."""
import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
assets = {}
if os.getenv('VSE_LOCAL_ASSETS'):
    node = next(n for n in ast.parse((ROOT/'app/web_assets.py').read_text()).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == 'current_web_assets')
    node.decorator_list = []
    scope = {'Request': object, 'STATIC_DIR': ROOT/'app/static', '_bundles': {}, '_bootstrap_mode': False,
             'store_bundle': lambda request, content, media_type: content}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'assets', 'exec'), scope)
    for name, mime in [('v19.js', 'text/javascript'), ('v19.css', 'text/css')]:
        assets[name] = (asyncio.run(scope['current_web_assets'](
            SimpleNamespace(method='GET', url=SimpleNamespace(path='/assets/'+name)), None)), mime)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page = browser.new_page(viewport={'width': 1366, 'height': 900})
    errors = []; requests = []; writes = []; submitted = set(); fail_bulk = [True]
    page.on('pageerror', lambda e: errors.append(str(e)))
    reason = {'rank': 1, 'reason': 'Possible mojibake', 'stream_count': 2, 'media_count': 2,
              'note': 'Uppercase accented words can be false positives.'}
    encoding_reason = {'rank': 2, 'reason': 'Non-UTF-8 source bytes', 'stream_count': 1, 'media_count': 1,
                       'note': 'Reversible legacy decoding is not necessarily damaged text.'}
    location = {'path': '/fixture/a.mkv', 'label': 'Example <movie>', 'source': 'embedded',
                'type_index': 0, 'line': 3, 'cue': '1', 'timing': '00:00:01,000 --> 00:00:02,000',
                'text': 'NÃO! <img src=x onerror=alert(1)>'}
    def respond(route):
        request = route.request; path = urlsplit(request.url).path
        if path == '/api/subtitle-autofix/encoding/queue-report':
            writes.append(path)
            assert page.locator('#global-busy-overlay').is_visible(), 'Bulk queue has no immediate waiting screen'
            assert page.locator('[data-damage-encoding-bulk]').first.is_disabled()
            page.evaluate('''() => {const button=document.querySelector('[data-damage-encoding-bulk]');button.click();button.onclick()}''')
            if fail_bulk[0]:
                fail_bulk[0] = False
                return route.fulfill(status=409, json={'detail': 'Fixture queue conflict; no job created'})
            kind = request.post_data_json['kind']
            submitted.add(kind)
            return route.fulfill(json={'preflight_id': 123, 'media_count': 40, 'stream_count': 80, 'accepted': True})
        if path == '/api/subtitle-autofix/options':
            return route.fulfill(json={'rules': [], 'note': 'SRT/SubRip text only.'})
        if request.method != 'GET':
            # These existing batch-read endpoints also use POST. Intercept
            # them, but do not mistake status/title lookups for media writes.
            if path not in {'/api/v79/tv/show-status','/api/v19/video-titles'}:
                writes.append(path)
            return route.fulfill(json={'ok': True, 'items': {}})
        if path.endswith('/reports/availability'):
            return route.fulfill(json={'reports': {'damaged': {'movies': True, 'tv': True}}, 'counts': {}})
        if path.endswith('/damaged-subtitles/examples'):
            requests.append(request.url)
            encoding = parse_qs(urlsplit(request.url).query).get('reason') == ['Non-UTF-8 source bytes']
            return route.fulfill(json={'reported_streams': 3, 'cached_streams': 2, 'unavailable_streams': 1,
                'unreproduced_count': 1, 'unreproduced': [location], 'distinct_examples': 1,
                'examples': [{'rank': 1, 'example': 'Windows-1252 (inferred)' if encoding else 'ÃO',
                              'quickfix_media_count': 40 if encoding else 0,
                              'occurrences': 80 if encoding else 12, 'stream_count': 80 if encoding else 2,
                              'media_count': 40 if encoding else 2, 'locations': [location]}]})
        if path == '/api/v89/preflight/123':
            return route.fulfill(json={'id': 123, 'status': 'pending', 'operation_type': 'subtitle_encoding_quickfix'})
        if path.endswith('/reports/damaged-subtitles'):
            kind = parse_qs(urlsplit(request.url).query)['kind'][0]
            if kind in submitted:
                return route.fulfill(json={'items': [], 'title_count': 0, 'media_count': 0})
            item = {'title': 'Example', 'paths': ['/fixture/a.mkv', '/fixture/b.mkv'],
                    'media_count': 2, 'damaged_subtitle_count': 2,
                    'streams': [{'damage': 'Non-UTF-8 source bytes'}]}
            if 'kind=tv' in request.url:
                item['episodes'] = [{'path': '/fixture/a.mkv', 'episode': 'S01E01', 'streams': item['streams']},
                                    {'path': '/fixture/b.mkv', 'episode': 'S01E02', 'streams': [{}]}]
            return route.fulfill(json={'items': [item], 'title_count': 1, 'media_count': 2,
                'damage_summary': {'reason_count': 2, 'stream_count': 2, 'reasons': [reason, encoding_reason]}})
        return route.continue_()
    page.route('**/api/**', respond)
    def serve(route):
        body, mime = assets[Path(urlsplit(route.request.url).path).name]
        route.fulfill(body=body, content_type=mime)
    for name in assets: page.route('**/assets/'+name, serve)
    page.goto('http://127.0.0.1:8383/', wait_until='domcontentloaded')
    page.wait_for_function('typeof window.renderDamageSummary === "function"')
    page.evaluate('''window.editorCalls=[];openReportMediaEditor=(...args)=>editorCalls.push(args.slice(0,3))''')
    page.locator('[data-page=reports]').click()
    page.wait_for_function('!document.querySelector("#reports").classList.contains("reports-checking")')
    dialog = page.locator('#image-subtitle-report-dialog')
    for kind in ['movies', 'tv']:
        # Invoke the actual report button handler even when fixture visibility is cached.
        page.evaluate('(kind)=>document.querySelector(`[data-damaged-subtitle-report="${kind}"]`).click()', kind)
        try:
            page.wait_for_selector('.damage-reason')
        except Exception:
            print('Report diagnostic', kind, errors, page.evaluate('''()=>({
              button:document.querySelector('[data-damaged-subtitle-report="movies"]').outerHTML,
              dialog:document.querySelector('#image-subtitle-report-dialog').textContent.slice(-2500),
              open:document.querySelector('#image-subtitle-report-dialog').open
            })'''))
            raise
        before = len(requests)
        page.locator('.damage-reason > summary').first.click()
        page.wait_for_selector('.damage-example')
        assert len(requests) == before+1
        assert 'without a usable cache' in dialog.inner_text()
        page.locator('.damage-example > summary').click()
        assert page.locator('.damage-location img').count() == 0
        assert 'NÃO!' in page.locator('.damage-location pre').first.inner_text()
        page.locator('[data-damage-example-edit]').first.click()
        assert page.evaluate('editorCalls.at(-1)[0]') == '/fixture/a.mkv'
        assert page.evaluate('editorCalls.at(-1)[2].length') == 2
        assert page.locator('[data-damage-example-autofix]').count()>0
        assert not page.locator('[data-damage-example-encoding]').count(), 'Quickfix leaked into another reason'
        assert not page.locator('[data-damage-encoding-bulk]').count(), 'Bulk quickfix leaked into another reason'
        assert page.locator('[data-damage-autofix]').count()>0
        page.locator('[data-damage-example-autofix]').first.click()
        review = page.locator('#subtitle-autofix-review-dialog')
        page.wait_for_function("document.querySelector('#subtitle-autofix-review-dialog [data-af-body]')?.textContent.includes('No enabled rule matches')")
        assert review.is_visible() and dialog.is_visible(), 'Review did not open above its report'
        assert not page.locator('[data-af-apply]').count(), 'Repair offered without rule/approval'
        review.locator('[data-af-close]').click()
        page.wait_for_function("!document.body.classList.contains('app-busy')")
        if kind=='tv':
            page.locator('.report-show-group > summary').first.click()
        page.locator('[data-damage-autofix]').first.click()
        page.wait_for_function("document.querySelector('#subtitle-autofix-review-dialog [data-af-body]')?.textContent.includes('No enabled rule matches')")
        assert review.is_visible() and dialog.is_visible()
        review.locator('[data-af-close]').click()
        page.wait_for_function("!document.body.classList.contains('app-busy')")
        page.locator('.damage-reason > summary').first.click()
        page.locator('.damage-reason > summary').first.click()
        page.wait_for_timeout(100)
        assert len(requests) == before+1
        page.locator('.damage-reason > summary').nth(1).click()
        page.wait_for_selector('[data-damage-example-encoding]', state='attached')
        page.locator('.damage-reason').nth(1).locator('.damage-example > summary').first.click()
        page.locator('[data-damage-example-encoding]').first.click()
        page.wait_for_function("document.querySelector('#autofix-review-title').textContent==='Subtitle UTF-8 quickfix'")
        assert 'Normalize legacy' in review.inner_text() and not page.locator('[data-af-apply]').count()
        review.locator('[data-af-close]').click()
        page.wait_for_function("!document.body.classList.contains('app-busy')")
        assert page.locator('[data-damage-encoding]').count()==1
        page.locator('[data-damage-encoding]').first.click()
        page.wait_for_function("document.querySelector('#subtitle-autofix-review-dialog').open")
        assert 'Normalize legacy' in review.inner_text()
        review.locator('[data-af-close]').click()
        page.wait_for_function("!document.body.classList.contains('app-busy')")
        page.set_viewport_size({'width': 430, 'height': 800})
        assert dialog.evaluate('e=>e.scrollWidth<=e.clientWidth+2')
        bulk = page.locator('[data-damage-encoding-bulk]')
        assert bulk.count()==1 and '40 media' in bulk.inner_text(), 'Bulk only covers example locations'
        assert 'No individual approvals' in dialog.inner_text()
        if kind=='movies':
            bulk.click()
            page.wait_for_function("!document.querySelector('#global-busy-overlay')")
            assert bulk.is_enabled()
            assert 'Fixture queue conflict' in page.locator('[data-encoding-bulk-status]').inner_text()
        before_bulk = len(writes)
        bulk.click()
        page.wait_for_function("!document.querySelector('#global-busy-overlay')")
        page.wait_for_function("document.querySelector('#image-subtitle-report-dialog [data-report-items]').textContent.includes('No damaged SRT')")
        assert len(writes)==before_bulk+1, 'Duplicate click queued twice'
        assert not review.is_visible(), 'Bulk requested individual approval'
        dialog.locator('[data-report-close]').last.click()
        # Dialog close dispatches its cleanup event asynchronously. Let it
        # finish before the next scripted click (as a real user would).
        page.wait_for_timeout(200)
        page.set_viewport_size({'width': 1366, 'height': 900})
    assert not errors, errors
    assert writes == ['/api/subtitle-autofix/encoding/queue-report']*3, writes
    browser.close()
print('PASS: movie/TV evidence and individual reviews; Windows-1252-only whole-group bulk queue, immediate busy/double-click guard, rejection/retry, queued-report removal, no approvals, narrow layout; all writes intercepted')
