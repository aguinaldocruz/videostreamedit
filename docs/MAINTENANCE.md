# Maintenance and release checklist

## Runtime map

The container starts `uvicorn app.v86:app`. The application is assembled through the versioned modules imported by that module; the version numbers are compatibility boundaries from the project's iterative development history, not separate services. The active chain includes the Plex/catalog layer, stream editor, task queue, reports, dashboard, and index queues. Do not delete an apparently old `vNN` module without first checking imports and route registration.

`app/main.py` is the original standalone prototype and is not the production entrypoint. `Dockerfile.orig` and other generated/runtime files must not be committed.

## Verification

Run before a release or pull request:

```console
python3 -m compileall -q app tools language-id
docker compose config -q
docker compose build
docker compose up -d
curl -fsS http://127.0.0.1:8383/api/health
git diff --check
```

Use disposable media for mutation tests. Confirm the container can see the exact Plex paths and that `/config` is writable.

## Persistent data

Back up `/config/videostreamedit.db` and `/config/plex-token.key` together. The database contains the Plex catalog, preferences, learned values, task history, notes, and index state. Never commit `/config`, media, model caches, or logs.

## Queue and index maintenance

The generic task queue handles media edits and long-running inspections. Core, subtitle, and preview work are separate incremental queues. Failed jobs should be inspected before retrying; signature errors mean the media changed after the request was created. HTML cleanup always passes preflight validation.

If a report appears stale, use the relevant index check or POST `/api/v19/reports/html-subtitles/revalidate?kind=movies|tv|all`; this requeues subtitle inspection for the current report entries rather than deleting the database.

## Documentation policy

Every persistent setting, queue type, API behavior, or destructive media operation change must update `README.md`, the relevant document under `docs/`, and the safety notes in `CONTRIBUTING.md`. Keep examples free of private Plex URLs, tokens, and real media paths.

## Verified legacy modules

A static dependency audit from `app.v86` found three versioned modules that are not imported by the production chain: `app/v3.py`, `app/v4.py`, and `app/v6.py`. They are retained intentionally as historical development entrypoints because users may still invoke them manually; they are not loaded by the Docker image. `app/main.py` is likewise a standalone prototype. They should only be removed in a deliberate major-version cleanup after confirming no external deployment references them.
