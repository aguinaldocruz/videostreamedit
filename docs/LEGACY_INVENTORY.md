# Legacy inventory — post-foundation redesign

Generated 2026-09-18. Evidence-only inventory; verified inactive modules/assets were removed only after cross-reference checks.

## Rules

- “Not referenced” means only that the active `/assets/v19.css` or `/assets/v19.js` assembly does not name the file directly.
- A file is not safe to delete solely from this list; older routes, tests, browser cache, or compatibility paths may still reference it.
- Candidates require route/search evidence and a passing smoke/approval gate before removal.

## Static assets

| Asset | Active assembly signal | Repository references | Example references |
|---|---|---:|---|
| `v10-progress.css` | active bundle | 7 | app/v8.py, app/v15.py, app/v13.py |
| `v10-progress.js` | active bundle | 7 | app/v8.py, app/v15.py, app/v13.py |
| `v11-plex.css` | active bundle | 6 | app/v15.py, app/v13.py, app/v16.py |
| `v11-plex.js` | active bundle | 6 | app/v15.py, app/v13.py, app/v16.py |
| `v12-context.css` | active bundle | 6 | app/v15.py, app/v13.py, app/v16.py |
| `v12-context.js` | active bundle | 6 | app/v15.py, app/v13.py, app/v16.py |
| `v14-brand.css` | active bundle | 4 | app/v15.py, app/v16.py, app/v19.py |
| `v15-path.css` | active bundle | 3 | app/v15.py, app/v16.py, app/v19.py |
| `v15-path.js` | active bundle | 3 | app/v15.py, app/v16.py, app/v19.py |
| `v16-bulk.css` | active bundle | 2 | app/v16.py, app/v19.py |
| `v16-bulk.js` | active bundle | 2 | app/v16.py, app/v19.py |
| `v17-value-picker.css` | not directly assembled | 0 | none found |
| `v17-value-picker.js` | not directly assembled | 0 | none found |
| `v18-value-popup.css` | active bundle | 2 | app/v16.py, app/v19.py |
| `v18-value-popup.js` | active bundle | 2 | app/v16.py, app/v19.py |
| `v19-titles.css` | active bundle | 1 | app/v19.py |
| `v19-titles.js` | active bundle | 1 | app/v19.py |
| `v2.css` | not directly assembled | 104 | app/v8.py, app/v28.py, app/v7.py |
| `v2.js` | not directly assembled | 104 | app/v8.py, app/v28.py, app/v7.py |
| `v20-clone.css` | active bundle | 1 | app/v19.py |
| `v20-clone.js` | active bundle | 1 | app/v19.py |
| `v21-navigation.js` | active bundle | 1 | app/v19.py |
| `v22-bulk-clone.css` | active bundle | 1 | app/v19.py |
| `v22-bulk-clone.js` | active bundle | 1 | app/v19.py |
| `v23-session-changes.css` | active bundle | 1 | app/v19.py |
| `v23-session-changes.js` | active bundle | 1 | app/v19.py |
| `v25-removal-safety.js` | active bundle | 1 | app/v19.py |
| `v25-template-history.css` | active bundle | 1 | app/v19.py |
| `v25-template-history.js` | active bundle | 1 | app/v19.py |
| `v26-stream-layout.css` | active bundle | 1 | app/v19.py |
| `v27-fast-defaults.css` | active bundle | 1 | app/v19.py |
| `v27-fast-defaults.js` | active bundle | 1 | app/v19.py |
| `v28-movie-import.css` | active bundle | 1 | app/v19.py |
| `v28-movie-import.js` | active bundle | 1 | app/v19.py |
| `v29-change-highlights.css` | active bundle | 1 | app/v19.py |
| `v29-change-highlights.js` | active bundle | 1 | app/v19.py |
| `v3.css` | active bundle | 62 | docs/MAINTENANCE.md, app/v8.py, app/v7.py |
| `v3.js` | not directly assembled | 62 | docs/MAINTENANCE.md, app/v8.py, app/v7.py |
| `v30-destination-order.css` | active bundle | 1 | app/v19.py |
| `v30-destination-order.js` | active bundle | 1 | app/v19.py |
| `v31-destination-browser.css` | active bundle | 1 | app/v19.py |
| `v31-destination-browser.js` | active bundle | 1 | app/v19.py |
| `v32-output-folder.css` | active bundle | 1 | app/v19.py |
| `v32-output-folder.js` | active bundle | 1 | app/v19.py |
| `v33-global-busy.css` | active bundle | 2 | app/v19.py, app/static/v95-global-busy.css |
| `v33-global-busy.js` | active bundle | 2 | app/v19.py, app/static/v95-global-busy.css |
| `v36-inline-combobox.css` | active bundle | 1 | app/v19.py |
| `v36-inline-combobox.js` | active bundle | 1 | app/v19.py |
| `v37-filename.css` | active bundle | 1 | app/v19.py |
| `v37-filename.js` | active bundle | 1 | app/v19.py |
| `v38-movie-filters.css` | active bundle | 1 | app/v19.py |
| `v38-movie-filters.js` | not directly assembled | 1 | app/v19.py |
| `v39-movie-index.css` | active bundle | 1 | app/v19.py |
| `v39-movie-index.js` | active bundle | 1 | app/v19.py |
| `v4.css` | active bundle | 59 | docs/MAINTENANCE.md, app/v8.py, app/v7.py |
| `v4.js` | not directly assembled | 59 | docs/MAINTENANCE.md, app/v8.py, app/v7.py |
| `v40-track-suggestions.css` | active bundle | 1 | app/v19.py |
| `v40-track-suggestions.js` | not directly assembled | 1 | app/v19.py |
| `v41-track-suggestions.js` | not directly assembled | 0 | none found |
| `v42-track-suggestions.js` | active bundle | 1 | app/v19.py |
| `v44-prompt-settings.css` | active bundle | 1 | app/v19.py |
| `v44-prompt-settings.js` | active bundle | 1 | app/v19.py |
| `v45-keyboard-navigation.js` | active bundle | 1 | app/v19.py |
| `v46-navigation-pending.css` | active bundle | 1 | app/v19.py |
| `v46-navigation-pending.js` | active bundle | 1 | app/v19.py |
| `v47-suggestion-shortcut.js` | active bundle | 1 | app/v19.py |
| `v48-setup-tabs.css` | active bundle | 1 | app/v19.py |
| `v48-setup-tabs.js` | active bundle | 1 | app/v19.py |
| `v49-stream-preview.css` | active bundle | 1 | app/v19.py |
| `v49-stream-preview.js` | not directly assembled | 1 | app/v19.py |
| `v5.css` | active bundle | 53 | docs/REDESIGN_IMPLEMENTATION_CHARTER.md, app/v8.py, app/v28.py |
| `v5.js` | active bundle | 53 | docs/REDESIGN_IMPLEMENTATION_CHARTER.md, app/v8.py, app/v28.py |
| `v50-stream-preview.css` | active bundle | 1 | app/v19.py |
| `v50-stream-preview.js` | not directly assembled | 1 | app/v19.py |
| `v51-subtitle-properties.css` | active bundle | 1 | app/v19.py |
| `v51-subtitle-properties.js` | active bundle | 1 | app/v19.py |
| `v54-split-index.css` | active bundle | 1 | app/v19.py |
| `v54-split-index.js` | active bundle | 1 | app/v19.py |
| `v56-background-index.js` | active bundle | 1 | app/v19.py |
| `v58-manual-audio-name.css` | active bundle | 1 | app/v19.py |
| `v58-manual-audio-name.js` | active bundle | 1 | app/v19.py |
| `v60-preview-layout.css` | active bundle | 1 | app/v19.py |
| `v61-preview-overflow.css` | active bundle | 1 | app/v19.py |
| `v62-escape-close.js` | active bundle | 1 | app/v19.py |
| `v63-index-controls.css` | active bundle | 1 | app/v19.py |
| `v63-index-controls.js` | active bundle | 1 | app/v19.py |
| `v65-task-queue.css` | active bundle | 1 | app/v19.py |
| `v65-task-queue.js` | active bundle | 1 | app/v19.py |
| `v66-close-pending.js` | active bundle | 1 | app/v19.py |
| `v67-index-schedules.css` | active bundle | 1 | app/v19.py |
| `v67-index-schedules.js` | active bundle | 1 | app/v19.py |
| `v68-plex-sync.css` | active bundle | 1 | app/v19.py |
| `v68-plex-sync.js` | active bundle | 1 | app/v19.py |
| `v69-stream-preview.css` | active bundle | 1 | app/v19.py |
| `v69-stream-preview.js` | active bundle | 1 | app/v19.py |
| `v7-addon.css` | active bundle | 8 | app/v8.py, app/v7.py, app/v15.py |
| `v7-addon.js` | not directly assembled | 8 | app/v8.py, app/v7.py, app/v15.py |
| `v71-pending-html.js` | active bundle | 1 | app/v19.py |
| `v72-preview-html-detection.js` | active bundle | 1 | app/v19.py |
| `v75-rename-refresh.js` | active bundle | 1 | app/v19.py |
| `v76-apply-queue.js` | active bundle | 1 | app/v19.py |
| `v77-bulk-track-name.css` | active bundle | 1 | app/v19.py |
| `v77-bulk-track-name.js` | active bundle | 1 | app/v19.py |
| `v78-change-requested.css` | active bundle | 1 | app/v19.py |
| `v78-change-requested.js` | active bundle | 1 | app/v19.py |
| `v79-season-filters.css` | active bundle | 1 | app/v19.py |
| `v79-season-filters.js` | active bundle | 1 | app/v19.py |
| `v8-addon.css` | active bundle | 7 | app/v8.py, app/v15.py, app/v13.py |
| `v8-addon.js` | active bundle | 7 | app/v8.py, app/v15.py, app/v13.py |
| `v81-performance.js` | active bundle | 1 | app/v19.py |
| `v82-movie-streams.css` | active bundle | 1 | app/v19.py |
| `v82-movie-streams.js` | active bundle | 1 | app/v19.py |
| `v83-media-review.css` | active bundle | 1 | app/v19.py |
| `v83-media-review.js` | active bundle | 1 | app/v19.py |
| `v84-activity.css` | active bundle | 1 | app/v19.py |
| `v84-activity.js` | active bundle | 1 | app/v19.py |
| `v85-remove-cycle.css` | active bundle | 1 | app/v19.py |
| `v85-remove-cycle.js` | active bundle | 1 | app/v19.py |
| `v86-tasks-layout.css` | active bundle | 1 | app/v19.py |
| `v86-tasks-layout.js` | active bundle | 1 | app/v19.py |
| `v87-language-region.css` | active bundle | 1 | app/v19.py |
| `v87-language-region.js` | active bundle | 1 | app/v19.py |
| `v88-reports.css` | active bundle | 1 | app/v19.py |
| `v88-reports.js` | active bundle | 1 | app/v19.py |
| `v89-design-tokens.css` | active bundle | 2 | docs/REDESIGN_IMPLEMENTATION_CHARTER.md, app/v19.py |
| `v9-session.js` | active bundle | 6 | app/v8.py, app/v15.py, app/v13.py |
| `v90-stream-properties.css` | active bundle | 1 | app/v19.py |
| `v91-listings.css` | active bundle | 1 | app/v19.py |
| `v92-dashboard-setup.css` | active bundle | 1 | app/v19.py |
| `v93-movie-import.css` | active bundle | 1 | app/v19.py |
| `v94-preview.css` | active bundle | 1 | app/v19.py |
| `v95-global-busy.css` | active bundle | 1 | app/v19.py |
| `v96-dialogs.css` | active bundle | 1 | app/v19.py |
| `v97-accessibility.css` | active bundle | 1 | app/v19.py |

## Next review

1. Search each candidate across all Python, HTML, JavaScript, CSS, tests, and documentation references.
2. Confirm no compatibility endpoint assembles or serves it.
3. Remove only in a separate approved change, then run compile, Compose, smoke, recovery, approval, and performance gates.
