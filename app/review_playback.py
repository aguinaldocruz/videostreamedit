"""Disposable, demand-paced review sessions. Never creates a catalog preview cache."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.v83 import app, _review_metadata, _review_plan, _subtitle_file, TEXT_SUBTITLE_CODECS
from app.v28 import authorized_import_file
from app.subtitle_cache_worker import cached_or_extract_track

ROOT = Path(os.getenv('MEDIA_STREAM_DIR', '/tmp/vse-media-streams')) / 'interactive'
SLOTS = threading.BoundedSemaphore(max(1, min(3, int(os.getenv('MEDIA_REVIEW_WORKERS', '2')))))
LOCK = threading.RLock()
SESSIONS: dict[str, dict] = {}
CANCELLED: dict[str, float] = {}
RESERVE = 512 * 1024**2
SESSION_LIMIT = 512 * 1024**2


class PlaybackRequest(BaseModel):
    path: str
    session_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    audio_index: int | None = Field(default=None, ge=0)
    audio_stage: str | None = None
    mode: str = Field(default='av', pattern=r'^(av|audio|video)$')
    start: float = Field(default=0, ge=0)
    force_aac: bool = False


class Heartbeat(BaseModel):
    position: float = Field(ge=0)


def close_session(identity: str):
    with LOCK:
        CANCELLED[identity] = time.monotonic()
        session = SESSIONS.get(identity)
        if session:
            session['cancel'].set()
            process = session.get('process')
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGCONT)
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


def _pump(session: dict):
    process = session['process']
    for line in process.stdout:
        if line.startswith('out_time_us='):
            try:
                session['produced'] = max(0, int(line.split('=', 1)[1]) / 1000000)
            except ValueError:
                pass


def playback_command(request: PlaybackRequest, media: Path, directory: Path, plan: dict, staged: Path | None):
    command = ['nice', '-n', '10', 'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
               '-threads', '2', '-readrate', '3', '-ss', str(request.start), '-i', str(media)]
    if staged:
        command += ['-ss', str(request.start), '-i', str(staged)]
    if request.mode in ('av', 'video'):
        command += ['-map', '0:v:0']
        command += ['-c:v', 'copy'] if plan['video_copy'] else ['-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p', '-force_key_frames', 'expr:gte(t,n_forced*4)']
    if request.mode in ('av', 'audio'):
        command += ['-map', '1:a:0' if staged else f'0:a:{request.audio_index}']
        command += ['-c:a', 'copy'] if plan['audio_copy'] and not request.force_aac else ['-c:a', 'aac', '-profile:a', 'aac_low', '-ac', '2', '-ar', '48000', '-b:a', '192k', '-threads', '2']
    command += ['-sn', '-dn', '-avoid_negative_ts', 'make_zero', '-progress', 'pipe:1', '-stats_period', '0.5',
                '-f', 'hls', '-hls_time', '4', '-hls_list_size', '18', '-hls_delete_threshold', '3',
                '-hls_flags', 'delete_segments+temp_file', '-hls_segment_filename', str(directory / 'part_%06d.ts'), str(directory / 'index.m3u8')]
    return command


def _worker(session: dict, request: PlaybackRequest, media: Path, plan: dict, staged: Path | None):
    directory = session['directory']
    acquired = False
    try:
        while not session['cancel'].is_set():
            if time.monotonic() - session['access'] > 90:
                return
            if SLOTS.acquire(timeout=.5):
                acquired = True
                break
        if not acquired:
            return
        session['phase'] = 'buffering'
        session['message'] = ('Copying compatible video' if plan['video_copy'] else 'Converting video for browser playback') + ('; copying audio' if plan['audio_copy'] else '; converting audio to AAC stereo') + '; waiting for first complete segment/keyframe'
        if shutil.disk_usage(ROOT).free < RESERVE:
            raise RuntimeError('Playback paused: insufficient temporary disk space')
        directory.mkdir(mode=0o700)
        with (directory / 'error.log').open('w') as error:
            process = subprocess.Popen(playback_command(request, media, directory, plan, staged), stdout=subprocess.PIPE,
                                       stderr=error, text=True, start_new_session=True)
            session['process'] = process
            threading.Thread(target=_pump, args=(session,), daemon=True).start()
            paused = False
            while process.poll() is None:
                if session['cancel'].wait(.25) or time.monotonic() - session['access'] > 90:
                    break
                if shutil.disk_usage(ROOT).free < RESERVE or sum(p.stat().st_size for p in directory.iterdir() if p.is_file()) > SESSION_LIMIT:
                    raise RuntimeError('Playback stopped at the temporary-space safety limit')
                want_pause = session['produced'] > session['position'] + 45
                if want_pause != paused:
                    os.killpg(process.pid, signal.SIGSTOP if want_pause else signal.SIGCONT)
                    paused = want_pause
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGCONT)
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            elif process.returncode:
                raise RuntimeError((directory / 'error.log').read_text()[-1200:] or 'Playback preparation failed')
        if acquired:
            SLOTS.release()
            acquired = False
        # Finished producers still have a short buffer the viewer may use.
        while not session['cancel'].wait(2) and time.monotonic() - session['access'] < 90:
            pass
    except Exception as exc:
        session['error'] = str(exc)
    finally:
        process = session.get('process')
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGCONT)
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            except ProcessLookupError:
                pass
        if acquired:
            SLOTS.release()
        shutil.rmtree(directory, ignore_errors=True)
        session['done'] = True


@app.post('/api/review/playback')
def create_playback(request: PlaybackRequest):
    media = authorized_import_file(request.path)
    metadata = _review_metadata(media)
    audios = [s for s in metadata.get('streams', []) if s.get('codec_type') == 'audio']
    if request.mode in ('av', 'audio') and (request.audio_index is None or request.audio_index >= len(audios)) and not request.audio_stage:
        raise HTTPException(400, 'Select an available audio stream')
    if request.mode in ('av', 'video') and not any(s.get('codec_type') == 'video' for s in metadata.get('streams', [])):
        raise HTTPException(400, 'This media has no video stream')
    staged = None
    if request.audio_stage:
        from app.review_audio import checked_audio
        staged, _ = checked_audio(request.audio_stage, str(media))
    plan = _review_plan(metadata, request.mode, request.audio_index, [], 'none', None)
    if staged:
        plan['audio_copy'] = True
    if request.force_aac:
        plan['audio_copy'] = False
    total = float((metadata.get('format') or {}).get('duration') or 0)
    request.start = min(request.start, max(0, total - .5)) if total else request.start
    ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK:
        # Keep the request queue bounded too; do not accumulate abandoned producers.
        for identity, stamp in list(CANCELLED.items()):
            if time.monotonic() - stamp > 180:
                CANCELLED.pop(identity, None)
        if request.session_id in CANCELLED:
            raise HTTPException(409, 'Playback request was cancelled')
        for identity, item in list(SESSIONS.items()):
            if item.get('done') and time.monotonic() - item['access'] > 90:
                SESSIONS.pop(identity, None)
        if request.session_id in SESSIONS or sum(not s.get('done') for s in SESSIONS.values()) >= 6:
            raise HTTPException(429, 'Review capacity is busy; close another player and retry')
        session = {'directory': ROOT / request.session_id, 'cancel': threading.Event(), 'access': time.monotonic(),
                   'position': 0, 'produced': 0, 'error': None, 'done': False, 'start': request.start,
                   'phase': 'waiting', 'message': 'Waiting for an available playback worker (bounded server capacity)'}
        SESSIONS[request.session_id] = session
    threading.Thread(target=_worker, args=(session, request, media, plan, staged), daemon=True).start()
    return {'session_id': request.session_id, 'duration': total, 'start': request.start, 'plan': plan,
            'manifest_url': f'/api/review/playback/{request.session_id}/index.m3u8'}


@app.get('/api/review/playback/{identity}/status')
def playback_status(identity: str):
    with LOCK:
        session = SESSIONS.get(identity)
    if not session:
        raise HTTPException(404, 'Playback session expired')
    session['access'] = time.monotonic()
    playlist = session['directory'] / 'index.m3u8'
    try:
        ready = playlist.exists() and '#EXTINF:' in playlist.read_text()
    except FileNotFoundError:
        ready = False
    return {'ready': ready, 'error': session['error'], 'done': session['done'], 'buffered_seconds': round(session['produced'], 1),
            'phase': session['phase'], 'message': session['message']}


@app.post('/api/review/playback/{identity}/heartbeat')
def heartbeat(identity: str, request: Heartbeat):
    session = SESSIONS.get(identity)
    if session:
        session['access'] = time.monotonic()
        session['position'] = request.position
    return {'active': bool(session and not session['done'])}


@app.delete('/api/review/playback/{identity}')
def cancel_playback(identity: str):
    if not re.fullmatch('[a-f0-9]{32}', identity):
        raise HTTPException(400, 'Invalid playback session')
    close_session(identity)
    return {'cancelled': True}


@app.get('/api/review/playback/{identity}/{filename}')
def playback_file(identity: str, filename: str):
    session = SESSIONS.get(identity)
    if not session or not re.fullmatch(r'(index\.m3u8|part_\d+\.ts)', filename):
        raise HTTPException(404, 'Playback buffer expired')
    session['access'] = time.monotonic()
    target = session['directory'] / filename
    if not target.is_file():
        raise HTTPException(404, 'Playback buffer no longer contains this position; seek again')
    return FileResponse(target, media_type='application/vnd.apple.mpegurl' if filename.endswith('m3u8') else 'video/mp2t', headers={'Cache-Control': 'no-store'})


@app.get('/api/review/subtitle')
def subtitle_text(path: str, source: str, index: int | None = None, external_path: str | None = None):
    media = authorized_import_file(path)
    if source not in ('embedded', 'external', 'staged'):
        raise HTTPException(400, 'Select a text subtitle')
    if source == 'embedded':
        subs = [s for s in _review_metadata(media).get('streams', []) if s.get('codec_type') == 'subtitle']
        if index is None or index < 0 or index >= len(subs):
            raise HTTPException(400, 'Subtitle no longer exists')
        if subs[index].get('codec_name') not in TEXT_SUBTITLE_CODECS:
            raise HTTPException(422, 'Image subtitles cannot be overlaid. Select a text subtitle or turn subtitles off.')
    from fastapi.responses import Response
    if source in ('embedded', 'external'):
        try:
            selected = cached_or_extract_track(
                media, source, index if source == 'embedded' else -1,
                str(external_path or '') if source == 'external' else '',
                _review_metadata(media),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        converted = subprocess.run(
            ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'srt', '-i', 'pipe:0',
             '-f', 'webvtt', 'pipe:1'],
            input=selected.text.encode('utf-8'), capture_output=True, timeout=30,
        )
        if converted.returncode or not converted.stdout:
            raise HTTPException(422, 'Cached subtitle could not be converted to WebVTT')
        return Response(converted.stdout, media_type='text/vtt')
    import tempfile
    with tempfile.TemporaryDirectory(prefix='vse-text-') as directory:
        name = _subtitle_file(media, source, index, external_path, Path(directory))
        return Response((Path(directory) / name).read_bytes(), media_type='text/vtt')


@app.on_event('startup')
def clean_abandoned_playback():
    ROOT.mkdir(parents=True, exist_ok=True)
    # Only our UUID directories, never arbitrary directories in /tmp.
    for directory in ROOT.iterdir():
        if directory.is_dir() and re.fullmatch('[a-f0-9]{32}', directory.name):
            shutil.rmtree(directory)
    # Former complete-VOD buffers used UUID directories directly under this
    # same dedicated media-stream root. They are disposable too after restart.
    for directory in ROOT.parent.iterdir():
        if directory.is_dir() and re.fullmatch('[a-f0-9]{32}', directory.name):
            shutil.rmtree(directory)


@app.on_event('shutdown')
def stop_playback():
    for identity in list(SESSIONS):
        close_session(identity)


@app.get('/assets/review-hls.js')
def hls_asset():
    return FileResponse(Path(__file__).parent / 'static' / 'vendor' / 'hls.min.js', media_type='text/javascript')
