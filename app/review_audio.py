"""User-requested AAC preparation; only audio is staged, media stays unchanged."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from app.v83 import app, _review_metadata
from app.v28 import authorized_import_file
from app.v11 import connection
import app.v65 as tasks

ROOT = Path(os.getenv('MEDIA_REVIEW_AUDIO_STAGE', '/data/review-audio'))


def fingerprint(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as source:
        digest.update(source.read(256 * 1024))
        source.seek(max(0, stat.st_size - 256 * 1024))
        digest.update(source.read(256 * 1024))
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'edges': digest.hexdigest()}


@app.on_event('startup')
def initialize_audio_stages():
    ROOT.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS review_audio_stage (
            id TEXT PRIMARY KEY, path TEXT NOT NULL, audio_index INTEGER NOT NULL,
            fingerprint_json TEXT NOT NULL, metadata_json TEXT NOT NULL, status TEXT NOT NULL,
            task_id INTEGER, action TEXT, draft_session TEXT, error TEXT,
            output_fingerprint_json TEXT, temporary_path TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        db.execute('ALTER TABLE review_audio_stage ADD COLUMN IF NOT EXISTS output_fingerprint_json TEXT')
        db.execute('ALTER TABLE review_audio_stage ADD COLUMN IF NOT EXISTS temporary_path TEXT')
        interrupted = [dict(row) for row in db.execute("SELECT * FROM review_audio_stage WHERE status IN ('building','applying')").fetchall()]
    for row in interrupted:
        media = Path(row['path'])
        try:
            actual = fingerprint(media)
            if actual == json.loads(row['output_fingerprint_json'] or '{}'):
                integration_complete([type('Change', (), {'stage_id': row['id']})()])
            elif actual == json.loads(row['fingerprint_json']):
                with connection() as db:
                    db.execute("UPDATE review_audio_stage SET status=?,error='Interrupted before commit; safe to retry' WHERE id=?", ('draft' if row['draft_session'] else 'queued', row['id']))
            else:
                with connection() as db:
                    db.execute("UPDATE review_audio_stage SET status='failed',error='Media changed during interrupted approval; review before retry' WHERE id=?", (row['id'],))
            temporary = Path(row['temporary_path'] or '')
            if temporary.parent == media.parent and temporary.name.startswith('.'+media.stem+'.') and '.vse' in temporary.name:
                temporary.unlink(missing_ok=True)
        except OSError:
            # Keep recovery evidence if a media mount is unavailable.
            continue
    with connection() as db:
        terminal = db.execute("SELECT id FROM review_audio_stage WHERE status IN ('applied','rejected')").fetchall()
    for row in terminal:
        if len(row['id']) == 32 and all(c in '0123456789abcdef' for c in row['id']):
            shutil.rmtree(ROOT / row['id'], ignore_errors=True)


def get_stage(identity: str) -> dict:
    if len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
        raise HTTPException(400, 'Invalid audio stage')
    with connection() as db:
        row = db.execute('SELECT * FROM review_audio_stage WHERE id=?', (identity,)).fetchone()
    if not row:
        raise HTTPException(404, 'Staged audio not found')
    return dict(row)


def checked_audio(identity: str, path: str) -> tuple[Path, dict]:
    row = get_stage(identity)
    if row['path'] != path or row['status'] not in ('ready', 'draft', 'queued', 'applying'):
        raise HTTPException(409, 'This audio is not ready for this media')
    if fingerprint(Path(path)) != json.loads(row['fingerprint_json']):
        raise HTTPException(409, 'Media changed since audio preparation. Reject this stage and prepare a fresh version.')
    audio = ROOT / identity / 'audio.m4a'
    if not audio.is_file():
        raise HTTPException(409, 'Staged audio file is missing; prepare again')
    return audio, row


class PrepareAudio(BaseModel):
    path: str
    audio_index: int = Field(ge=0)


class ApproveAudio(BaseModel):
    action: Literal['add', 'replace'] = 'add'
    draft_session: str | None = None


@app.post('/api/review/audio')
def prepare_audio(request: PrepareAudio):
    from app.v86 import assert_media_editable
    media = authorized_import_file(request.path)
    assert_media_editable(str(media))
    if media.suffix.lower() not in ('.mkv', '.mka'):
        raise HTTPException(422, 'Permanent AAC integration currently requires Matroska; temporary AAC playback remains available.')
    metadata = _review_metadata(media)
    audios = [s for s in metadata.get('streams', []) if s.get('codec_type') == 'audio']
    if request.audio_index >= len(audios):
        raise HTTPException(400, 'Audio stream no longer exists')
    identity = uuid.uuid4().hex
    signature = json.dumps(fingerprint(media), sort_keys=True)
    with connection() as db:
        # Serialize concurrent button requests for the same path without blocking other media.
        db.execute('SELECT pg_advisory_xact_lock(hashtext(?))', ('review-audio:'+str(media),))
        existing = db.execute("SELECT id,task_id FROM review_audio_stage WHERE path=? AND audio_index=? AND fingerprint_json=? AND status IN ('pending','running','ready','draft','queued','applying')", (str(media), request.audio_index, signature)).fetchone()
        if existing:
            return dict(existing)
        db.execute('INSERT INTO review_audio_stage(id,path,audio_index,fingerprint_json,metadata_json,status) VALUES(?,?,?,?,?,?)',
                   (identity, str(media), request.audio_index, signature, json.dumps(audios[request.audio_index]), 'pending'))
    try:
        task = tasks.enqueue('review_audio_prepare', {'stage_id': identity}, 'Prepare AAC stereo · '+media.name)
        with connection() as db:
            db.execute('UPDATE review_audio_stage SET task_id=? WHERE id=?', (task['id'], identity))
        return {'id': identity, 'task_id': task['id']}
    except Exception as exc:
        with connection() as db:
            db.execute("UPDATE review_audio_stage SET status='failed',error=? WHERE id=?", (str(exc), identity))
        raise


def process_audio(task_id: int, payload: dict):
    identity = payload['stage_id']
    row = get_stage(identity)
    if row['status'] == 'rejected':
        return {'skipped': 'Rejected by user'}
    media = authorized_import_file(row['path'])
    if fingerprint(media) != json.loads(row['fingerprint_json']):
        raise RuntimeError('Media changed after AAC preparation was requested')
    directory = ROOT / identity
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / 'audio.m4a'
    temporary = directory / 'audio.partial.m4a'
    process = None
    try:
        with connection() as db:
            db.execute("UPDATE review_audio_stage SET status='running',error=NULL WHERE id=?", (identity,))
        duration = float((_review_metadata(media).get('format') or {}).get('duration') or 0)
        required = int(duration * 26000) + 512 * 1024**2
        if shutil.disk_usage(ROOT).free < required:
            raise RuntimeError('Insufficient space for staged AAC audio and safety reserve')
        tasks.update_progress(task_id, 0, 3, 'Converting selected audio to AAC-LC stereo; original media is unchanged')
        command = ['nice', '-n', '15', 'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '2',
                   '-i', str(media), '-map', f"0:a:{row['audio_index']}", '-vn', '-sn', '-c:a', 'aac', '-profile:a', 'aac_low',
                   '-ac', '2', '-ar', '48000', '-b:a', '192k', '-threads', '2', '-movflags', '+faststart', '-y', str(temporary)]
        with (directory / 'error.log').open('w') as error:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=error)
            started = time.monotonic()
            while process.poll() is None:
                if tasks.queue_shutdown.wait(.5) or time.monotonic() - started > 7200:
                    raise RuntimeError('Audio preparation interrupted; retry the task')
                if shutil.disk_usage(ROOT).free < 512 * 1024**2:
                    raise RuntimeError('Audio preparation stopped to preserve free disk space')
            if process.returncode:
                raise RuntimeError((directory / 'error.log').read_text()[-1600:] or 'Audio decoder failed')
        tasks.update_progress(task_id, 1, 3, 'Verifying AAC format, duration and source fingerprint')
        from app.v2 import probe
        result = probe(temporary)
        audio = next((s for s in result.get('streams', []) if s.get('codec_type') == 'audio'), {})
        if audio.get('codec_name') != 'aac' or audio.get('channels') != 2:
            raise RuntimeError('Converted audio failed AAC stereo verification')
        if duration and abs(float((result.get('format') or {}).get('duration') or 0) - duration) > max(5, duration * .01):
            raise RuntimeError('Converted audio duration differs from the media; manual investigation required')
        if fingerprint(media) != json.loads(row['fingerprint_json']):
            raise RuntimeError('Media changed during conversion; staged output discarded')
        os.replace(temporary, target)
        with connection() as db:
            db.execute("UPDATE review_audio_stage SET status='ready',error=NULL WHERE id=?", (identity,))
        tasks.update_progress(task_id, 3, 3, 'AAC stereo ready for comparison and approval in Media Review')
        return {'stage_id': identity, 'path': str(media), 'status': 'awaiting approval'}
    except Exception as exc:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        temporary.unlink(missing_ok=True)
        with connection() as db:
            db.execute("UPDATE review_audio_stage SET status='failed',error=? WHERE id=?", (str(exc), identity))
        raise


tasks.TASK_HANDLERS['review_audio_prepare'] = process_audio


@app.get('/api/review/audio')
def list_audio(path: str):
    media = authorized_import_file(path)
    with connection() as db:
        rows = [dict(row) for row in db.execute("SELECT s.id,s.audio_index,s.status,s.task_id,s.action,s.draft_session,coalesce(t.error,s.error) AS error,t.status AS task_status FROM review_audio_stage s LEFT JOIN task_queue t ON t.id=s.task_id WHERE s.path=? AND s.status NOT IN ('applied','rejected') ORDER BY s.created_at DESC", (str(media),)).fetchall()]
        for row in rows:
            if row['status'] in ('pending','running') and (row['task_status'] in ('failed','cancelled') or (row['task_id'] and row['task_status'] is None)):
                db.execute("UPDATE review_audio_stage SET status='failed',error=? WHERE id=?", (row['error'] or 'Preparation task cancelled', row['id']))
                row['status'] = 'failed'
    return {'items': [dict(row) for row in rows]}


@app.post('/api/review/audio/{identity}/approve')
def approve_audio(identity: str, request: ApproveAudio):
    row = get_stage(identity)
    from app.v86 import assert_media_editable
    assert_media_editable(row['path'])
    checked_audio(identity, row['path'])
    if request.draft_session:
        from app.v79 import _tv_edit_session, TvEditOperationRequest, add_tv_edit_operation
        session = _tv_edit_session(request.draft_session)
        with connection() as db:
            media = db.execute('SELECT library_key,show_title FROM plex_media WHERE path=?', (row['path'],)).fetchone()
        if session['status'] != 'open' or not media or session['show_id'] != f"{media['library_key']}:{media['show_title'] or 'Unknown show'}":
            raise HTTPException(409, 'This episode is not in the open TV-show draft')
    with connection() as db:
        current = db.execute('SELECT status,task_id FROM review_audio_stage WHERE id=? FOR UPDATE', (identity,)).fetchone()
        if current['status'] != 'ready':
            raise HTTPException(409, 'This audio has already been submitted or is not ready')
        db.execute('UPDATE review_audio_stage SET status=?,action=?,draft_session=? WHERE id=?',
                   ('draft' if request.draft_session else 'queued', request.action, request.draft_session, identity))
    edit = {'path': row['path'], 'audio_compatibility': [{'stage_id': identity, 'action': request.action}],
            **{key:'__preserve__' for key in ('default_audio','forced_audio','default_subtitle','forced_subtitle')}}
    try:
        if request.draft_session:
            result = add_tv_edit_operation(request.draft_session, TvEditOperationRequest(path=row['path'], operation={'audio_compatibility': edit['audio_compatibility']}))
            return {'draft': True, **result}
        task = tasks.enqueue('media_edit', {'edit': edit}, 'Approve AAC stereo · '+Path(row['path']).name)
        with connection() as db:
            db.execute('UPDATE review_audio_stage SET task_id=? WHERE id=?', (task['id'], identity))
        return {'task_id': task['id'], 'queued': True}
    except Exception:
        with connection() as db:
            db.execute("UPDATE review_audio_stage SET status='ready',draft_session=NULL WHERE id=?", (identity,))
        raise


@app.delete('/api/review/audio/{identity}')
def reject_audio(identity: str):
    row = get_stage(identity)
    authorized_import_file(row['path'])
    with connection() as db:
        locked = db.execute('SELECT status,task_id FROM review_audio_stage WHERE id=? FOR UPDATE', (identity,)).fetchone()
        task = db.execute('SELECT status FROM task_queue WHERE id=?', (locked['task_id'],)).fetchone() if locked['task_id'] else None
        failed_approval = locked['status'] == 'queued' and task and task['status'] in ('failed','cancelled')
        if locked['status'] not in ('ready', 'failed') and not failed_approval:
            raise HTTPException(409, 'Cannot reject audio while it is running, queued or referenced by a draft')
        db.execute("UPDATE review_audio_stage SET status='rejected' WHERE id=?", (identity,))
    shutil.rmtree(ROOT / identity, ignore_errors=True)
    return {'rejected': True}


def release_draft_audio(session_id: str):
    with connection() as db:
        db.execute("UPDATE review_audio_stage SET status='ready',draft_session=NULL WHERE draft_session=? AND status='draft'", (session_id,))


def resolve_integrations(path: Path, changes: list):
    result = []
    seen = set()
    for change in changes:
        audio, row = checked_audio(change.stage_id, str(path))
        if row['status'] not in ('queued', 'draft') or row['action'] != change.action:
            raise HTTPException(409, 'Audio integration has not been approved')
        if row['audio_index'] in seen:
            raise HTTPException(409, 'Approve only one conversion per source audio stream')
        seen.add(row['audio_index'])
        result.append((change, audio, row))
    return result


def integration_complete(changes: list):
    for change in changes:
        with connection() as db:
            db.execute("UPDATE review_audio_stage SET status='applied',error=NULL WHERE id=?", (change.stage_id,))
        shutil.rmtree(ROOT / change.stage_id, ignore_errors=True)


def integration_intent(changes: list, temporary: Path):
    signature = json.dumps(fingerprint(temporary), sort_keys=True)
    with connection() as db:
        for change in changes:
            db.execute("UPDATE review_audio_stage SET status='applying',output_fingerprint_json=?,temporary_path=? WHERE id=?", (signature, str(temporary), change.stage_id))


def integration_building(changes: list, temporary: Path):
    with connection() as db:
        for change in changes:
            db.execute("UPDATE review_audio_stage SET status='building',temporary_path=? WHERE id=?", (str(temporary), change.stage_id))


def integration_failed(changes: list):
    with connection() as db:
        for change in changes:
            row = db.execute('SELECT path,fingerprint_json FROM review_audio_stage WHERE id=?', (change.stage_id,)).fetchone()
            if row and fingerprint(Path(row['path'])) == json.loads(row['fingerprint_json']):
                db.execute("UPDATE review_audio_stage SET status=CASE WHEN draft_session IS NULL THEN 'queued' ELSE 'draft' END WHERE id=? AND status IN ('building','applying')", (change.stage_id,))
