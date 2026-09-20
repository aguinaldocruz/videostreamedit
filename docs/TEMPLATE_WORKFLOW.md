# Stream-change template workflow

Stream-change templates are stored durably in PostgreSQL and mirrored to browser history as a fallback. Templates contain the canonical before/after stream state and only the effective patch is applied.

## Immediate and queued use

- **Apply bulk clone** performs the existing immediate workflow.
- **Queue bulk clone** performs preflight, captures one media signature per item, and creates one resumable `media_edit` task per compatible media.
- Missing media, no-op changes, and media with another active change are reported as skipped or conflicts before queue insertion.
- Existing queued/running work is never silently overwritten.

## Durable template API

- `GET /api/v25/templates?include_disabled=true`
- `POST /api/v25/templates`
- `PUT /api/v25/templates/{id}`
- `DELETE /api/v25/templates/{id}` or `DELETE /api/v25/templates`
- `POST /api/v25/templates/{id}/use`

## Bulk run tracking

`POST /api/v25/templates/bulk-queue` returns a `run_id`, queued task IDs, skipped entries, and conflicts. Use `GET /api/v25/template-runs/{run_id}` to inspect child task progress and final status.

Disabled templates remain available for maintenance but are excluded from clone suggestions. Existing local browser templates remain usable during the transition.
