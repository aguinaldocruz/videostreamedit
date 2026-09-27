"""Small isolated regression tests for TV-show draft note commits.

Run inside the application image: python -m scripts.test_tv_draft_notes
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import app.v79 as drafts


class TvDraftNoteCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "notes.sqlite"
        self.episodes = [Path(self.temp.name) / f"episode-{index}.mkv" for index in (1, 2)]
        for episode in self.episodes:
            episode.touch()
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE plex_media(path TEXT PRIMARY KEY,kind TEXT,library_key TEXT,show_title TEXT)")
            db.execute("CREATE TABLE media_notes(entity_type TEXT,entity_key TEXT,note TEXT,reviewed INTEGER,plex_sync_change INTEGER,final_version INTEGER,updated_at TEXT,PRIMARY KEY(entity_type,entity_key))")
            db.executemany("INSERT INTO plex_media VALUES(?,'episode','library','Example')", [(str(path),) for path in self.episodes])
            db.execute("INSERT INTO media_notes VALUES('tv','library:Example','Show note',0,0,0,'')")
            db.execute("INSERT INTO media_notes VALUES('tv',?,'Episode note',0,1,0,'')", (f"episode:{self.episodes[0]}",))

        @contextmanager
        def test_connection():
            db = sqlite3.connect(self.database)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

        self.original_connection = drafts.connection
        drafts.connection = test_connection
        self.addCleanup(lambda: setattr(drafts, "connection", self.original_connection))

    def note(self, key: str) -> sqlite3.Row:
        with drafts.connection() as db:
            return db.execute("SELECT * FROM media_notes WHERE entity_type='tv' AND entity_key=?", (key,)).fetchone()

    def test_show_final_is_committed_to_episodes_without_losing_notes(self) -> None:
        drafts._commit_tv_note_changes("library:Example", "@show:library:Example", {"final_version": True})
        self.assertEqual(self.note("library:Example")["note"], "Show note")
        self.assertEqual(self.note("library:Example")["reviewed"], 1)
        for episode in self.episodes:
            self.assertEqual(self.note(f"episode:{episode}")["final_version"], 1)
            self.assertEqual(self.note(f"episode:{episode}")["reviewed"], 1)
        self.assertEqual(self.note(f"episode:{self.episodes[0]}")["note"], "Episode note")
        self.assertEqual(self.note(f"episode:{self.episodes[0]}")["plex_sync_change"], 1)

    def test_episode_unfreeze_clears_parent_final(self) -> None:
        drafts._commit_tv_note_changes("library:Example", "@show:library:Example", {"final_version": True})
        drafts._commit_tv_note_changes("library:Example", str(self.episodes[0]), {"final_version": False})
        self.assertEqual(self.note("library:Example")["final_version"], 0)
        self.assertEqual(self.note(f"episode:{self.episodes[0]}")["final_version"], 0)
        self.assertEqual(self.note(f"episode:{self.episodes[1]}")["final_version"], 1)

    def test_note_only_change_does_not_change_final_status(self) -> None:
        drafts._commit_tv_note_changes("library:Example", "@show:library:Example", {"note": "Revised note", "reviewed": True})
        self.assertEqual(self.note("library:Example")["note"], "Revised note")
        self.assertEqual(self.note("library:Example")["reviewed"], 1)
        self.assertEqual(self.note("library:Example")["final_version"], 0)
        self.assertEqual(self.note(f"episode:{self.episodes[0]}")["final_version"], 0)
        self.assertIsNone(self.note(f"episode:{self.episodes[1]}"))


if __name__ == "__main__":
    unittest.main()
