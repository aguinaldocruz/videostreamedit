"""Read-only Reports UI regressions against a running app (Playwright required).

Report responses are fixtures; every non-GET API request is blocked.
Run: python scripts/test_reports_ui.py [http://127.0.0.1:8383]
"""
import json
import sys
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        errors = []
        writes = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        keys = ("image", "damaged", "html", "language", "confidence", "duplicate_audio",
                "duplicate_subtitle", "uncommon", "forced", "audio_only", "english_only", "external_only", "video_titles")

        def respond(route):
            request = route.request
            if request.url.endswith('/api/v19/video-titles'):
                return route.fulfill(json={"items": {}})
            if request.url.endswith('/api/v79/tv/show-status'):
                return route.fulfill(json={"needs_attention": False, "reasons": []})
            if request.method != "GET":
                writes.append(request.url)
                return route.fulfill(status=409, json={"detail": "Test prohibits writes"})
            url = request.url
            if '/reports/video-titles?' in url:
                episode = {"title": "Episode", "episode": "S01E01", "path": "/fixture/video.mkv", "video_title": "Old video title"}
                items = [{"title": "Video show", "episodes": [episode], "media_count": 1}] if 'kind=tv' in url else [episode]
                return route.fulfill(json={"items": items, "title_count": 1, "media_count": 1})
            if "/reports/availability" in url:
                return route.fulfill(json={"reports": {key: {"tv": True, "movies": True} for key in keys},
                                           "counts": {key: {"tv": 2, "movies": 2} for key in keys}})
            if "/reports/html-subtitles?" in url:
                if 'kind=tv' in url:
                    episodes = [{"path": f"/fixture/ep{i}.mkv", "episode": f"S01E0{i} · Episode {i}", "media_count": 1,
                                 "html_subtitle_count": 1, "streams": [{"path": f"/fixture/ep{i}.mkv", "type_index": 0}]} for i in (1, 2)]
                    return route.fulfill(json={"items": [{"title": "Example show", "media_count": 2, "html_subtitle_count": 2,
                                                         "episodes": episodes, "streams": [s for ep in episodes for s in ep['streams']]}],
                                               "title_count": 1, "media_count": 2})
                rows = [{"title": f"Movie {i:03}", "media_count": 1, "html_subtitle_count": 1,
                         "streams": [{"path": f"/fixture/{i}.mkv", "type_index": 0}]} for i in range(205)]
                return route.fulfill(json={"items": rows, "title_count": 205, "media_count": 205})
            if "/reports/english-only?" in url:
                return route.fulfill(json={"items": [{"title": "Movie A", "path": "/fixture/a.mkv", "mode": "audio_only_english"},
                                                      {"title": "Movie B", "path": "/fixture/b.mkv", "mode": "audio_only_english"}],
                                           "title_count": 2, "media_count": 2})
            if "/reports/duplicate-languages?" in url:
                return route.fulfill(json={"items": [{"title": "Duplicate movie", "path": "/fixture/d.mkv",
                                                      "duplicates": [{"language": "en", "count": 2}]}],
                                           "languages": ["en"], "title_count": 1, "media_count": 1})
            if "/reports/uncommon-languages?" in url:
                return route.fulfill(json={"items": [{"title": 'Quoted "movie"', "path": '/fixture/"quoted".mkv',
                                                      "streams": [{"language": "es", "stream_type": "audio"}]}],
                                           "title_count": 1, "media_count": 1})
            if "/reports/damaged-subtitles?" in url:
                return route.fulfill(json={"items": [{"title": "Damaged movie", "paths": ["/fixture/d.mkv"], "damaged_subtitle_count": 1}],
                                           "title_count": 1, "media_count": 1})
            if "/reports/subtitle-no-confidence?" in url:
                return route.fulfill(json={"items": [{"path": "/fixture/c.mkv", "title": "Uncertain", "source": "embedded", "type_index": 0,
                                                      "status": "no_confidence", "reason": "Short text"}], "media_count": 1, "stream_count": 1})
            route.continue_()

        page.route("**/api/**", respond)
        page.goto(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8383", wait_until="domcontentloaded")
        page.locator('[data-page="reports"]').click()
        page.wait_for_function("!document.querySelector('#reports').classList.contains('reports-checking')")
        assert page.locator('#reports .reports-category').count() == 4
        dialog = page.locator('#image-subtitle-report-dialog')

        def open_report(selector):
            page.locator(selector).click()
            page.wait_for_function("!document.querySelector('#image-subtitle-report-dialog .reports-loading')")

        def close_report():
            dialog.locator('[data-report-close]').last.click()

        open_report('[data-video-title-report="tv"]')
        assert dialog.locator('[data-video-all]').is_visible()
        assert dialog.locator('[data-video-show]').is_visible()
        dialog.locator('.report-show-group summary').click()
        assert dialog.locator('[data-video-remove]').is_visible()
        assert dialog.locator('[data-report-html-fix]').is_hidden()
        close_report()
        open_report('[data-video-title-report="movies"]')
        assert dialog.locator('[data-video-remove]').is_visible()
        close_report()

        open_report('[data-html-subtitle-report="movies"]')
        assert dialog.locator('[data-report-html-fix]').is_visible()
        assert dialog.locator('.report-item:visible').count() == 100
        dialog.locator('.report-load-more').click()
        assert dialog.locator('.report-item:visible').count() == 200
        dialog.locator('.report-result-toolbar input').fill('Movie 204')
        assert dialog.locator('.report-item:visible').count() == 1
        page.evaluate('window.resumeReportView(window.captureReportView())')
        page.wait_for_function("!document.querySelector('#image-subtitle-report-dialog .reports-loading')")
        assert dialog.locator('.report-result-toolbar input').input_value() == 'Movie 204'
        assert dialog.locator('.report-item:visible').count() == 1
        close_report()

        open_report('[data-english-only-report="movies"]')
        assert not dialog.locator('[data-report-html-fix]').is_visible()
        page.evaluate("window.openReportMediaEditor=(path,label,nav)=>{window.testReportNavigation={path,nav}}")
        dialog.locator('[data-english-index="0"]').last.click()
        assert len(page.evaluate('window.testReportNavigation.nav')) == 2
        close_report()

        open_report('[data-html-subtitle-report="tv"]')
        assert dialog.locator('.report-show-group').count() == 1
        dialog.locator('.report-result-toolbar input').fill('S01E02')
        assert dialog.locator('.report-item:visible').count() == 1
        dialog.locator('.report-item:visible [data-report-open]').click()
        assert page.evaluate('window.testReportNavigation.path') == '/fixture/ep2.mkv'
        assert len(page.evaluate('window.testReportNavigation.nav')) == 2
        close_report()

        open_report('[data-damaged-subtitle-report="movies"]')
        assert dialog.locator('.reports-loading').count() == 0
        assert dialog.locator('[data-damage-edit]').count() == 1
        close_report()

        open_report('[data-duplicate-language-report="movies:audio"]')
        assert dialog.locator('[data-dup-edit]').count() == 1
        close_report()

        open_report('[data-no-confidence-report="movies"]')
        assert dialog.locator('[data-no-confidence-controls]').is_visible()
        close_report()
        open_report('[data-english-only-report="movies"]')
        assert not dialog.locator('[data-no-confidence-controls]').is_visible()
        assert not dialog.locator('[data-report-html-fix]').is_visible()
        close_report()

        open_report('[data-uncommon-language-report="movies"]')
        assert dialog.locator('[data-uncommon-remove]').get_attribute('data-uncommon-remove') == '/fixture/"quoted".mkv'
        close_report()

        assert page.evaluate("""async () => {
            const pending = reportRequest('/api/v19/reports/english-only?kind=movies');
            window.reportEpoch++;
            try { await pending; return false; } catch (error) { return error.reportStale === true; }
        }""")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
        assert not writes, writes
        print(json.dumps({"status": "passed", "checks": "categories, action isolation, paging/search, navigation, damage/duplicates, confidence filters, narrow layout", "browser_errors": errors}))
        browser.close()


if __name__ == '__main__':
    main()
