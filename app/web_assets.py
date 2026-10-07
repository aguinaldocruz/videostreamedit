"""Single current web asset owner; immutable bundles cached per process."""
import hashlib
import json
from fastapi import Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from app.v2 import app, STATIC_DIR
from app import db_bootstrap
from app import system_timezone

_bundles = {}
_bootstrap_mode = not db_bootstrap.configured()
BOOTSTRAP_ASSETS = {'/assets/bootstrap.css':'v100-db-bootstrap.css',
                    '/assets/bootstrap.js':'v100-db-bootstrap.js'}


def cached_response(request, path):
    content, media_type, etag = _bundles[path]
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    candidates = [tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match", "").split(",")]
    if etag.removeprefix("W/") in candidates or "*" in candidates:
        return Response(status_code=304, headers=headers)
    return Response(content, media_type=media_type, headers=headers)


def store_bundle(request, content, media_type):
    encoded = content.encode("utf-8")
    etag = 'W/"' + hashlib.sha256(encoded).hexdigest() + '"'
    _bundles[request.url.path] = (encoded, media_type, etag)
    return cached_response(request, request.url.path)


@app.middleware("http")
async def current_web_assets(request: Request, call_next):
    path = request.url.path
    if _bootstrap_mode:
        # This mode is fixed for the process: saving credentials requires a
        # restart before normal routes and workers may use the target database.
        if request.method == 'GET' and path == '/':
            return HTMLResponse('''<!doctype html><html lang="en"><meta charset="utf-8">
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <title>VideoStreamEdit · Database setup</title>
                <link rel="stylesheet" href="/assets/bootstrap.css">
                <body><main><h1>VideoStreamEdit</h1><p>Connect PostgreSQL to begin.</p>
                <button onclick="location.reload()">Open database setup</button></main>
                <script src="/assets/bootstrap.js"></script></body></html>''',headers={'Cache-Control':'no-store'})
        if request.method == 'GET' and path in BOOTSTRAP_ASSETS:
            if path in _bundles:
                return cached_response(request,path)
            name=BOOTSTRAP_ASSETS[path]
            return store_bundle(request,(STATIC_DIR/name).read_text(),'text/css' if name.endswith('.css') else 'text/javascript')
        if path not in ('/api/bootstrap/status','/api/bootstrap/configure','/api/health') and not path.startswith('/brand/'):
            return JSONResponse({'detail':'Configure PostgreSQL and restart before using the application.'},status_code=503)
    if request.method == "GET" and path in _bundles:
        return cached_response(request, path)
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text()
        html = html.replace('<title>VideoStreamEdit</title><link rel="stylesheet" href="/app.css">', '<title>VideoStreamEdit · Stream Metadata Editor</title><meta name="theme-color" content="#14191f"><link rel="icon" href="/brand/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="32x32" href="/brand/favicon-32.png"><link rel="icon" type="image/png" sizes="16x16" href="/brand/favicon-16.png"><link rel="apple-touch-icon" sizes="180x180" href="/brand/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><link rel="stylesheet" href="/assets/v19.css">')
        html = html.replace('<header><h1>VideoStreamEdit</h1><div id="page-title" class="header-page-title"><h2>Movies</h2><p>Choose a movie to edit its audio and subtitle streams.</p></div><nav>', '<header><a class="brand" href="/" aria-label="VideoStreamEdit home"><img src="/brand/header-icon.png" width="48" height="48" alt=""><span><strong>VideoStreamEdit</strong><small>Stream metadata editor</small></span></a><div id="page-title" class="header-page-title"><h2>Movies</h2><p>Choose a movie to edit its audio and subtitle streams.</p></div><nav>')
        html = html.replace('<script src="/app.js"></script>', '<script>window.VSE_TIMEZONE='+json.dumps(system_timezone.selected_timezone())+';</script><script src="/assets/v19.js"></script>')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v19.css":
        names = ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css", "v11-plex.css", "v12-context.css", "v14-brand.css", "v15-path.css", "v16-bulk.css", "v18-value-popup.css", "v19-titles.css", "v20-clone.css", "v22-bulk-clone.css", "v23-session-changes.css", "v25-template-history.css", "v26-stream-layout.css", "v27-fast-defaults.css", "v28-movie-import.css", "v29-change-highlights.css", "v30-destination-order.css", "v31-destination-browser.css", "v32-output-folder.css", "v33-global-busy.css", "v36-inline-combobox.css", "v37-filename.css", "v38-movie-filters.css", "v39-movie-index.css", "v40-track-suggestions.css", "v44-prompt-settings.css", "v46-navigation-pending.css", "v48-setup-tabs.css", "v51-subtitle-properties.css", "v58-manual-audio-name.css", "v65-task-queue.css", "v67-index-schedules.css", "v68-plex-sync.css", "v78-change-requested.css", "v79-season-filters.css", "v82-movie-streams.css", "v83-media-review.css", "v84-activity.css", "v85-remove-cycle.css", "v86-tasks-layout.css", "v87-language-region.css", "v88-reports.css", "v89-design-tokens.css", "v90-stream-properties.css", "v91-listings.css", "v92-dashboard-setup.css", "v93-movie-import.css", "v95-global-busy.css", "v96-dialogs.css", "v97-accessibility.css", "v101-migrate-info.css", "v102-setup.css", "v103-tv-workspace.css", "v105-reports.css")
        names += ("stream-editor.css", "dashboard.css", "review-player.css", "subtitle-cache-schedule.css", "setup-workspace.css", "v108-subtitle-color.css", "subtitle-autofix.css", "subtitle-autofix-review.css")
        return store_bundle(request, "\n".join((STATIC_DIR / name).read_text() for name in names), "text/css")
    if request.method == "GET" and request.url.path == "/assets/v19.js":
        javascript = (STATIC_DIR / "system-timezone.js").read_text() + "\n" + (STATIC_DIR / "subtitle-html.js").read_text() + "\n" + (STATIC_DIR / "v33-global-busy.js").read_text() + "\n" + (STATIC_DIR / "v5.js").read_text().replace("'/api/movies'", "'/api/v19/movies'").replace("'/api/tv'", "'/api/v19/tv'")
        for name in ("v8-addon.js", "v9-session.js", "v10-progress.js", "v11-plex.js", "v12-context.js", "v15-path.js", "v16-bulk.js", "v18-value-popup.js", "v19-titles.js", "v20-clone.js", "v21-navigation.js", "v22-bulk-clone.js", "v23-session-changes.js", "v25-removal-safety.js", "v25-template-history.js", "v27-fast-defaults.js", "v28-movie-import.js", "v29-change-highlights.js", "v30-destination-order.js", "v31-destination-browser.js", "v32-output-folder.js", "v36-inline-combobox.js", "v37-filename.js", "v39-movie-index.js", "v42-track-suggestions.js", "v44-prompt-settings.js", "v45-keyboard-navigation.js", "v46-navigation-pending.js", "v47-suggestion-shortcut.js", "v48-setup-tabs.js", "v51-subtitle-properties.js", "v56-background-index.js", "v58-manual-audio-name.js", "v62-escape-close.js", "v65-task-queue.js", "v66-close-pending.js", "v67-index-schedules.js", "v68-plex-sync.js", "v71-pending-html.js", "v72-preview-html-detection.js", "v75-rename-refresh.js", "v76-apply-queue.js", "v78-change-requested.js", "v79-season-filters.js", "v82-movie-streams.js", "v83-media-review.js", "v84-activity.js", "v85-remove-cycle.js", "v86-tasks-layout.js", "v98-scheduled-tasks.js", "v99-backup.js", "v101-migrate-info.js", "v87-language-region.js", "v88-reports.js", "v102-setup.js", "v103-tv-workspace.js", "v104-tv-draft-notes.js", "v105-reports.js"):
            javascript += "\n" + (STATIC_DIR / name).read_text()
        javascript += "\n" + (STATIC_DIR / "stream-editor.js").read_text()
        javascript += "\n" + (STATIC_DIR / "subtitle-damage-report.js").read_text()
        javascript += "\n" + (STATIC_DIR / "dashboard.js").read_text()
        javascript += "\n" + (STATIC_DIR / "review-player.js").read_text()
        javascript += "\n" + (STATIC_DIR / "v106-timezone-setup.js").read_text()
        javascript += "\n" + (STATIC_DIR / "v107-final-open-edit.js").read_text()
        javascript += "\n" + (STATIC_DIR / "scheduled-task-actions.js").read_text()
        javascript += "\n" + (STATIC_DIR / "v108-subtitle-color.js").read_text()
        javascript += "\n" + (STATIC_DIR / "subtitle-autofix.js").read_text()
        javascript += "\n" + (STATIC_DIR / "subtitle-autofix-review.js").read_text()
        javascript = javascript.replace("/api/media/details", "/api/v13/media/details-fast").replace("/api/v11/plex/sync", "/api/v19/plex/sync").replace("/api/v7/media/edit", "/api/v43/media/edit")
        return store_bundle(request, javascript, "text/javascript")
    return await call_next(request)
