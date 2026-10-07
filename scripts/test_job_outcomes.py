"""Deleted media is terminal; unavailable storage is not confirmed deletion."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.job_outcomes import PermanentMediaMissing, require_media_file

with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    video = root / 'Video'
    video.mkdir()
    media = video / 'present.mkv'
    media.write_bytes(b'fixture')
    with patch('app.job_outcomes._mount_roots', return_value=(root, Path('/'))):
        assert require_media_file(media).st_size == 7
        for missing in (video / 'deleted.mkv', video / 'removed-show' / 'deleted.mkv'):
            try:
                require_media_file(missing)
            except PermanentMediaMissing as exc:
                assert 'No automatic retry' in str(exc)
            else:
                raise AssertionError('Deleted media did not produce a permanent outcome')
        try:
            require_media_file(root / 'OfflineVideo' / 'missing.mkv')
        except PermanentMediaMissing:
            raise AssertionError('Missing storage was classified as deleted media')
        except RuntimeError as exc:
            assert 'deletion is not confirmed' in str(exc)
        else:
            raise AssertionError('Storage outage ignored')
print('PASS: missing file/folder terminal outcome; unavailable storage remains recoverable')
