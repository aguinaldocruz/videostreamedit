# Video stream title report

Reports → Video metadata → Video stream titles provides Movies and TV Shows.
It lists indexed media with nonempty **video stream** titles, not container
titles or audio/subtitle track names. TV results expand into episode trees.
Final Version media, pending media changes/preflights, and pending/running
indexes are excluded. Setup → Appearance controls report visibility.

Queue removal is available for a movie/episode, a show's listed episodes,
or all results in the opened report (including paginated results). A confirmation
shows the target count. An asynchronous preflight validates current files and
skips already-empty titles before creating protected `media_edit` jobs.
Pending requests disappear from reports and become eligible again after
processing and reindexing if the finding remains.

Cleanup deletes the name property from every named video track in Matroska
files, preserving audio, subtitles, dispositions and the container title.
Non-Matroska cleanup is explicitly refused; there is no automatic remux fallback.
The existing media-job signature, LUW and core-index follow-up are reused.
Subtitle/audio language detection is not required by this metadata-only edit.

Validation: `python scripts/test_video_title_cleanup.py` and
`python scripts/test_reports_ui.py` (browser tests use report fixtures).
