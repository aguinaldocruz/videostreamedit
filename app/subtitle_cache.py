"""Complete text-subtitle cache storage.

This module deliberately does not extract subtitles. Callers must supply a
source signature covering the media's subtitle layout and external sidecars;
only a complete, verified set of tracks may be published for a media item.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.pg_compat import connect
from app.v2 import app


CACHE_FORMAT_VERSION = 1


@dataclass(frozen=True)
class TextSubtitle:
    source: str
    type_index: int
    external_path: str
    codec: str
    text: str

    @property
    def key(self) -> tuple[str, int, str]:
        return self.source, self.type_index, self.external_path


def ensure_subtitle_cache_schema() -> None:
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS subtitle_cache_media (
                path TEXT PRIMARY KEY,
                source_signature TEXT NOT NULL,
                format_version INTEGER NOT NULL,
                expected_tracks INTEGER NOT NULL,
                cached_tracks INTEGER NOT NULL,
                cached_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS subtitle_cache_track (
                path TEXT NOT NULL,
                source TEXT NOT NULL,
                type_index INTEGER NOT NULL,
                external_path TEXT NOT NULL DEFAULT '',
                codec TEXT NOT NULL,
                text_content TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                cached_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(path, source, type_index, external_path),
                FOREIGN KEY(path) REFERENCES subtitle_cache_media(path) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS subtitle_cache_track_media
                ON subtitle_cache_track(path);
            CREATE TABLE IF NOT EXISTS subtitle_cache_pending (
                path TEXT PRIMARY KEY,
                requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                revision INTEGER NOT NULL DEFAULT 1,
                retry_after BIGINT NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS subtitle_cache_pending_ready
                ON subtitle_cache_pending(retry_after,requested_at);
            CREATE TABLE IF NOT EXISTS subtitle_cache_failure (
                path TEXT PRIMARY KEY,
                error TEXT NOT NULL,
                failed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        prior_failures = db.execute(
            "SELECT path,last_error FROM subtitle_cache_pending WHERE last_error!=''"
        ).fetchall()
        if prior_failures:
            db.executemany(
                "INSERT INTO subtitle_cache_failure(path,error) VALUES(?,?) "
                "ON CONFLICT(path) DO NOTHING",
                [(row["path"], row["last_error"]) for row in prior_failures],
            )
            db.executemany(
                "DELETE FROM subtitle_cache_pending WHERE path=? AND last_error!=''",
                [(row["path"],) for row in prior_failures],
            )


@app.on_event("startup")
def initialize_subtitle_cache() -> None:
    ensure_subtitle_cache_schema()


def invalidate_media(path: str) -> None:
    """Remove the manifest and all tracks in one transaction."""
    with connect() as db:
        db.execute("DELETE FROM subtitle_cache_media WHERE path=?", (path,))


def invalidate_media_many(paths: list[str]) -> int:
    """Plex invalidates changed sources; core indexing decides which need work."""
    unique = list(dict.fromkeys(str(path) for path in paths if path))
    if not unique:
        return 0
    with connect() as db:
        db.executemany("DELETE FROM subtitle_cache_media WHERE path=?", [(path,) for path in unique])
    return len(unique)


def invalidate_and_enqueue_media_many(paths: list[str]) -> int:
    """Atomically retire changed media caches and make them priority work."""
    unique = list(dict.fromkeys(str(path) for path in paths if path))
    if not unique:
        return 0
    with connect() as db:
        db.executemany("DELETE FROM subtitle_cache_media WHERE path=?", [(path,) for path in unique])
        db.executemany(
            "INSERT INTO subtitle_cache_pending(path) VALUES(?) "
            "ON CONFLICT(path) DO UPDATE SET requested_at=CURRENT_TIMESTAMP,"
            "revision=subtitle_cache_pending.revision+1,retry_after=0,attempts=0,last_error=''",
            [(path,) for path in unique],
        )
        db.executemany("DELETE FROM subtitle_cache_failure WHERE path=?", [(path,) for path in unique])
    return len(unique)


def enqueue_media(path: str) -> None:
    """Give changed media priority over the background catalog cursor."""
    enqueue_media_many([path])


def enqueue_media_many(paths: list[str]) -> int:
    """Publish a Plex batch in one transaction, without one DB connection per file."""
    unique = list(dict.fromkeys(str(path) for path in paths if path))
    if not unique:
        return 0
    with connect() as db:
        db.executemany(
            "INSERT INTO subtitle_cache_pending(path) VALUES(?) "
            "ON CONFLICT(path) DO UPDATE SET requested_at=CURRENT_TIMESTAMP,revision=subtitle_cache_pending.revision+1,retry_after=0,attempts=0,last_error=''",
            [(path,) for path in unique],
        )
        db.executemany("DELETE FROM subtitle_cache_failure WHERE path=?", [(path,) for path in unique])
    return len(unique)


def prioritize_final_media(paths: list[str]) -> int:
    """Put newly finalized, uncached media ahead of a resumed catalog cursor.

    Existing pending requests retain their revision and retry state, but move
    ahead of ordinary changed-media requests. A complete cache needs no new
    work; source changes invalidate it through the normal media-change hooks.
    """
    unique = list(dict.fromkeys(str(path) for path in paths if path))
    if not unique:
        return 0
    queued = 0
    with connect() as db:
        for start in range(0, len(unique), 500):
            batch = unique[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            cached = db.execute(
                "SELECT path FROM subtitle_cache_media WHERE path IN (" + placeholders + ") "
                "AND format_version=? AND expected_tracks=cached_tracks",
                (*batch, CACHE_FORMAT_VERSION),
            ).fetchall()
            complete = {str(row["path"]) for row in cached}
            failures = db.execute(
                "SELECT path FROM subtitle_cache_failure WHERE path IN (" + placeholders + ")",
                batch,
            ).fetchall()
            failed = {str(row["path"]) for row in failures}
            without_subtitles = db.execute(
                "SELECT s.path FROM media_stream_index_state s WHERE s.path IN (" + placeholders + ") "
                "AND NOT EXISTS (SELECT 1 FROM media_stream_index i WHERE i.path=s.path "
                "AND i.stream_type IN ('subtitle','external'))",
                batch,
            ).fetchall()
            no_subs = {str(row["path"]) for row in without_subtitles}
            pending = [path for path in batch if path not in complete and path not in failed and path not in no_subs]
            if pending:
                db.executemany(
                    "INSERT INTO subtitle_cache_pending(path,requested_at) VALUES(?,'1970-01-01 00:00:00+00') "
                    "ON CONFLICT(path) DO UPDATE SET requested_at=excluded.requested_at",
                    [(path,) for path in pending],
                )
                queued += len(pending)
    return queued


def pending_revision(path: str) -> int | None:
    with connect() as db:
        row = db.execute("SELECT revision FROM subtitle_cache_pending WHERE path=?", (path,)).fetchone()
    return int(row["revision"]) if row else None


def next_pending_media(now_epoch: int) -> str | None:
    with connect() as db:
        row = db.execute(
            "SELECT path FROM subtitle_cache_pending WHERE retry_after<=? ORDER BY requested_at,path LIMIT 1",
            (now_epoch,),
        ).fetchone()
    return str(row["path"]) if row else None


def complete_pending_media(path: str, revision: int | None) -> None:
    if revision is None:
        return
    with connect() as db:
        db.execute("DELETE FROM subtitle_cache_pending WHERE path=? AND revision=?", (path, revision))


def record_media_failure(path: str, error: str, revision: int | None = None) -> bool:
    """Quarantine a cache failure until a real media-change hook requeues it."""
    with connect() as db:
        if revision is not None:
            removed = db.execute(
                "DELETE FROM subtitle_cache_pending WHERE path=? AND revision=?",
                (path, revision),
            ).rowcount
            if not removed:
                return False  # A newer change request owns this path.
        elif db.execute("SELECT 1 FROM subtitle_cache_pending WHERE path=?", (path,)).fetchone():
            return False  # The media changed while this catalog attempt ran.
        db.execute(
            "INSERT INTO subtitle_cache_failure(path,error,failed_at) VALUES(?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(path) DO UPDATE SET error=excluded.error,failed_at=CURRENT_TIMESTAMP",
            (path, error[:1000]),
        )
    return True


def failed_media() -> list[dict]:
    with connect() as db:
        rows = db.execute("""
            SELECT f.path,f.error,f.failed_at,p.kind,p.title,p.show_title,
                   p.season_number,p.episode_number
              FROM subtitle_cache_failure f
              LEFT JOIN plex_media p ON p.path=f.path
             ORDER BY f.failed_at DESC,f.path
        """).fetchall()
    return [dict(row) for row in rows]


def retry_failed_media(path: str | None = None) -> int:
    """Explicit user retry; changed-media hooks use enqueue_media_many instead."""
    with connect() as db:
        rows = db.execute(
            "SELECT path FROM subtitle_cache_failure" + (" WHERE path=?" if path is not None else ""),
            (path,) if path is not None else (),
        ).fetchall()
        paths = [str(row["path"]) for row in rows]
        if not paths:
            return 0
        db.executemany(
            "INSERT INTO subtitle_cache_pending(path) VALUES(?) "
            "ON CONFLICT(path) DO UPDATE SET requested_at=CURRENT_TIMESTAMP,"
            "revision=subtitle_cache_pending.revision+1,retry_after=0,attempts=0,last_error=''",
            [(path,) for path in paths],
        )
        db.executemany("DELETE FROM subtitle_cache_failure WHERE path=?", [(path,) for path in paths])
    return len(paths)


def fail_pending_media(path: str, error: str, now_epoch: int, revision: int | None = None) -> None:
    with connect() as db:
        row = db.execute("SELECT attempts,revision FROM subtitle_cache_pending WHERE path=?", (path,)).fetchone()
        if not row:
            return
        attempts = min(20, int(row["attempts"]) + 1)
        delay = min(3600, 60 * (2 ** min(attempts - 1, 6)))
        db.execute(
            "UPDATE subtitle_cache_pending SET attempts=?,retry_after=?,last_error=? "
            "WHERE path=? AND revision=?",
            (attempts, now_epoch + delay, error[:500], path, revision if revision is not None else row["revision"]),
        )


def publish_media(
    path: str,
    source_signature: str,
    expected_keys: set[tuple[str, int, str]],
    tracks: list[TextSubtitle],
) -> None:
    """Atomically replace a media's cache; never publish a partial set."""
    if not path or not source_signature:
        raise ValueError("Media path and source signature are required")
    keys = [track.key for track in tracks]
    if len(keys) != len(set(keys)) or set(keys) != expected_keys:
        raise ValueError("Cached subtitles do not match the complete expected track set")
    if any(track.source not in {"embedded", "external"} or not track.codec or not track.text for track in tracks):
        raise ValueError("Each cached track needs a source, codec, and complete nonempty text")
    with connect() as db:
        db.execute("DELETE FROM subtitle_cache_media WHERE path=?", (path,))
        db.execute(
            "INSERT INTO subtitle_cache_media(path,source_signature,format_version,expected_tracks,cached_tracks) "
            "VALUES(?,?,?,?,?)",
            (path, source_signature, CACHE_FORMAT_VERSION, len(expected_keys), len(tracks)),
        )
        for track in tracks:
            db.execute(
                "INSERT INTO subtitle_cache_track(path,source,type_index,external_path,codec,text_content,sha256) "
                "VALUES(?,?,?,?,?,?,?)",
                (path, track.source, track.type_index, track.external_path, track.codec, track.text,
                 hashlib.sha256(track.text.encode("utf-8")).hexdigest()),
            )


def publish_track(
    path: str,
    source_signature: str,
    expected_keys: set[tuple[str, int, str]],
    track: TextSubtitle,
) -> None:
    """Publish a read-through track without falsely marking the media complete."""
    if not path or not source_signature or track.key not in expected_keys or not track.text or not track.codec:
        raise ValueError("Invalid subtitle cache track or source manifest")
    with connect() as db:
        row = db.execute(
            "SELECT source_signature,format_version,expected_tracks FROM subtitle_cache_media WHERE path=?",
            (path,),
        ).fetchone()
        if row and (row["source_signature"] != source_signature or row["format_version"] != CACHE_FORMAT_VERSION
                    or row["expected_tracks"] != len(expected_keys)):
            db.execute("DELETE FROM subtitle_cache_media WHERE path=?", (path,))
            row = None
        if not row:
            db.execute(
                "INSERT INTO subtitle_cache_media(path,source_signature,format_version,expected_tracks,cached_tracks) "
                "VALUES(?,?,?,?,0)", (path, source_signature, CACHE_FORMAT_VERSION, len(expected_keys)),
            )
        db.execute(
            "INSERT INTO subtitle_cache_track(path,source,type_index,external_path,codec,text_content,sha256) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(path,source,type_index,external_path) "
            "DO UPDATE SET codec=excluded.codec,text_content=excluded.text_content,sha256=excluded.sha256,cached_at=CURRENT_TIMESTAMP",
            (path, track.source, track.type_index, track.external_path, track.codec, track.text,
             hashlib.sha256(track.text.encode("utf-8")).hexdigest()),
        )
        count = db.execute("SELECT count(*) AS n FROM subtitle_cache_track WHERE path=?", (path,)).fetchone()["n"]
        db.execute("UPDATE subtitle_cache_media SET cached_tracks=?,cached_at=CURRENT_TIMESTAMP WHERE path=?", (count, path))


def get_valid_track(path: str, source_signature: str, key: tuple[str, int, str]) -> TextSubtitle | None:
    """Read a selected cached subtitle even when other tracks remain uncached."""
    with connect() as db:
        manifest = db.execute(
            "SELECT source_signature,format_version FROM subtitle_cache_media WHERE path=?", (path,),
        ).fetchone()
        if not manifest or manifest["source_signature"] != source_signature or manifest["format_version"] != CACHE_FORMAT_VERSION:
            return None
        row = db.execute(
            "SELECT codec,text_content,sha256 FROM subtitle_cache_track "
            "WHERE path=? AND source=? AND type_index=? AND external_path=?", (path, *key),
        ).fetchone()
        if not row or hashlib.sha256(row["text_content"].encode("utf-8")).hexdigest() != row["sha256"]:
            return None
        return TextSubtitle(*key, row["codec"], row["text_content"])


def valid_cached_keys(path: str, source_signature: str) -> set[tuple[str, int, str]]:
    """Return checksum-verified cached tracks for one current media signature."""
    with connect() as db:
        manifest = db.execute(
            "SELECT source_signature,format_version FROM subtitle_cache_media WHERE path=?", (path,),
        ).fetchone()
        if not manifest or manifest["source_signature"] != source_signature or manifest["format_version"] != CACHE_FORMAT_VERSION:
            return set()
        rows = db.execute(
            "SELECT source,type_index,external_path,text_content,sha256 FROM subtitle_cache_track WHERE path=?",
            (path,),
        ).fetchall()
    return {
        (row["source"], row["type_index"], row["external_path"])
        for row in rows
        if hashlib.sha256(row["text_content"].encode("utf-8")).hexdigest() == row["sha256"]
    }


def manifest_complete(path: str, source_signature: str, expected_tracks: int) -> bool:
    """Cheap work-selection check that does not transfer cached text from PG."""
    with connect() as db:
        row = db.execute(
            "SELECT source_signature,format_version,expected_tracks,cached_tracks "
            "FROM subtitle_cache_media WHERE path=?", (path,),
        ).fetchone()
        if not row or row["source_signature"] != source_signature or row["format_version"] != CACHE_FORMAT_VERSION:
            return False
        if row["expected_tracks"] != expected_tracks or row["cached_tracks"] != expected_tracks:
            return False
        actual = db.execute("SELECT count(*) AS n FROM subtitle_cache_track WHERE path=?", (path,)).fetchone()["n"]
        return actual == expected_tracks


def cache_record_complete(path: str) -> bool:
    """Scheduled-work check; source changes are invalidated by index hooks."""
    with connect() as db:
        row = db.execute(
            "SELECT expected_tracks,cached_tracks FROM subtitle_cache_media "
            "WHERE path=? AND format_version=?", (path, CACHE_FORMAT_VERSION),
        ).fetchone()
        if not row or int(row["expected_tracks"]) != int(row["cached_tracks"]):
            return False
        count = db.execute("SELECT count(*) AS n FROM subtitle_cache_track WHERE path=?", (path,)).fetchone()["n"]
        return int(count) == int(row["expected_tracks"])


def missing_cache_paths(paths: list[str]) -> set[str]:
    """Select uncached catalog media in one DB read, excluding indexed no-sub media."""
    if not paths:
        return set()
    placeholders = ",".join("?" for _ in paths)
    with connect() as db:
        rows = db.execute(
            "SELECT p.path FROM plex_media p "
            "LEFT JOIN subtitle_cache_media c ON c.path=p.path "
            f"WHERE p.path IN ({placeholders}) "
            "AND (c.path IS NULL OR c.format_version!=? OR c.expected_tracks!=c.cached_tracks "
            "OR c.cached_tracks!=(SELECT count(*) FROM subtitle_cache_track t WHERE t.path=p.path)) "
            "AND (NOT EXISTS (SELECT 1 FROM media_stream_index_state s WHERE s.path=p.path) "
            "OR EXISTS (SELECT 1 FROM media_stream_index i WHERE i.path=p.path AND i.stream_type IN ('subtitle','external'))) ",
            (*paths, CACHE_FORMAT_VERSION),
        ).fetchall()
    return {str(row["path"]) for row in rows}


def get_valid_media(path: str, source_signature: str) -> list[TextSubtitle] | None:
    """Return complete text only when the caller's current source still matches."""
    with connect() as db:
        manifest = db.execute(
            "SELECT source_signature,format_version,expected_tracks,cached_tracks "
            "FROM subtitle_cache_media WHERE path=?", (path,),
        ).fetchone()
        if not manifest or manifest["source_signature"] != source_signature or manifest["format_version"] != CACHE_FORMAT_VERSION:
            return None
        rows = db.execute(
            "SELECT source,type_index,external_path,codec,text_content,sha256 "
            "FROM subtitle_cache_track WHERE path=? ORDER BY source,type_index,external_path", (path,),
        ).fetchall()
        if len(rows) != manifest["expected_tracks"] or len(rows) != manifest["cached_tracks"]:
            return None
        if any(hashlib.sha256(row["text_content"].encode("utf-8")).hexdigest() != row["sha256"] for row in rows):
            return None
        return [TextSubtitle(row["source"], row["type_index"], row["external_path"], row["codec"], row["text_content"]) for row in rows]
