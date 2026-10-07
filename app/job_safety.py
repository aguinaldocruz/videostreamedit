"""Durable task checkpoints and bounded, filesystem-wide write admission.

Receipts are not an instruction to repeat a mutation: recovery must first match
the recorded output. A prepared-but-unconfirmed mutation is refused for review.
"""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import os
import shutil
import threading
import time
import subprocess
import tempfile

from app.postgres_store import connection

_local = threading.local()


def widen_media_columns(db):
    """Idempotent startup migration, never an unconditional ALTER on a hot path."""
    db.execute("SELECT pg_advisory_xact_lock(hashtext('vse:media-widths-v3'))")
    columns = {
        'plex_media': ('size', 'modified', 'plex_added_at', 'plex_updated_at'),
        'plex_internal_change_scope': ('expected_size', 'expected_modified', 'created_at', 'expires_at'),
        'ocr_staged_backups': ('size_bytes',),
        'media_stream_index_state': ('size', 'modified_ns'),
        'media_video_title': ('size', 'modified_ns'),
        'subtitle_extended_media': ('size', 'modified'),
        'external_subtitle_index': ('size', 'modified_ns'),
    }
    for table, names in columns.items():
        for name in names:
            row = db.execute("SELECT data_type FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s AND column_name=%s", (table, name)).fetchone()
            if row and row['data_type'] == 'integer':
                db.execute(f'ALTER TABLE {table} ALTER COLUMN {name} TYPE BIGINT')


def initialize():
    with connection() as db:
        widen_media_columns(db)
        db.execute("""CREATE TABLE IF NOT EXISTS task_execution_receipts (
            task_id BIGINT NOT NULL REFERENCES task_queue(id) ON DELETE CASCADE,
            step TEXT NOT NULL, data JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(task_id,step))""")


def reconcile_orphans():
    """Retire abandoned waiting metadata only; never delete recovery artifacts."""
    from app.postgres_store import _lock_workflow_mutation
    with connection() as db:
        db.execute("SET LOCAL lock_timeout='3s'")
        db.execute("SET LOCAL statement_timeout='20s'")
        _lock_workflow_mutation(db)
        # A deleted job's unstarted LUW is not recovery work. Retire only
        # plans that never recorded a mutation; retain all journals/artifacts.
        retired_luws = db.execute("""UPDATE workflow_luws l SET status='cancelled',
            current_step='retired',finished_at=now(),updated_at=now(),
            error='Queue owner removed; unstarted media plan retired'
            WHERE l.status IN ('planned','preflighted','waiting')
            AND l.updated_at < now()-interval '5 minutes'
            AND NOT EXISTS (SELECT 1 FROM task_queue q WHERE replace(q.group_id,'-','')=replace(l.group_id::text,'-',''))
            AND NOT EXISTS (SELECT 1 FROM index_task_queue q WHERE replace(q.group_id,'-','')=replace(l.group_id::text,'-',''))
            AND NOT EXISTS (SELECT 1 FROM workflow_groups g WHERE g.group_id=l.group_id AND g.status NOT IN ('failed','cancelled'))
            AND NOT EXISTS (SELECT 1 FROM workflow_artifacts a WHERE a.group_id=l.group_id)
            AND NOT EXISTS (SELECT 1 FROM workflow_locks k WHERE k.group_id=l.group_id)
            AND NOT EXISTS (SELECT 1 FROM workflow_luw_locks k WHERE k.luw_id=l.luw_id)
            AND NOT EXISTS (SELECT 1 FROM workflow_stages s WHERE s.group_id=l.group_id AND s.status='running')
            AND NOT EXISTS (SELECT 1 FROM workflow_luw_journal j WHERE j.luw_id=l.luw_id AND (j.kind<>'operation_plan' OR j.committed))""").rowcount
        # Failed historical groups may retain originals, but a deleted child
        # must not continue to look runnable. Preserve the group and artifacts.
        db.execute("""UPDATE workflow_stages s SET status='cancelled',finished_at=now(),updated_at=now(),
            error='Queue owner removed; any recovery data remains retained'
            FROM workflow_groups g WHERE s.group_id=g.group_id AND g.status='failed'
            AND s.status='pending'
            AND NOT EXISTS (SELECT 1 FROM task_queue q WHERE q.id::text=s.payload->>'task_id'
                AND replace(q.group_id,'-','')=replace(g.group_id::text,'-','') AND q.task_type=s.task_type)
            AND NOT EXISTS (SELECT 1 FROM index_task_queue q WHERE q.id::text=s.payload->>'task_id'
                AND replace(q.group_id,'-','')=replace(g.group_id::text,'-','') AND 'index:'||q.job=s.task_type)
            AND NOT EXISTS (SELECT 1 FROM workflow_locks k WHERE k.group_id=g.group_id)
            AND NOT EXISTS (SELECT 1 FROM workflow_luws l WHERE l.group_id=g.group_id AND l.status IN ('locked','applying','verifying'))""")
        rows = db.execute("""SELECT g.group_id FROM workflow_groups g
            WHERE g.status IN ('pending','waiting','blocked') AND g.updated_at < now()-interval '5 minutes'
            AND NOT EXISTS (SELECT 1 FROM task_queue q WHERE replace(q.group_id,'-','')=replace(g.group_id::text,'-',''))
            AND NOT EXISTS (SELECT 1 FROM index_task_queue q WHERE replace(q.group_id,'-','')=replace(g.group_id::text,'-',''))
            AND NOT EXISTS (SELECT 1 FROM workflow_artifacts a WHERE a.group_id=g.group_id)
            AND NOT EXISTS (SELECT 1 FROM workflow_locks l WHERE l.group_id=g.group_id)
            AND NOT EXISTS (SELECT 1 FROM workflow_luws l WHERE l.group_id=g.group_id AND l.status IN ('locked','applying','verifying'))
            AND NOT EXISTS (SELECT 1 FROM workflow_luw_locks k JOIN workflow_luws l USING(luw_id) WHERE l.group_id=g.group_id)
            AND NOT EXISTS (SELECT 1 FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status='running')
            LIMIT 500 FOR UPDATE OF g SKIP LOCKED""").fetchall()
        ids = [r['group_id'] for r in rows]
        if ids:
            db.execute("UPDATE workflow_stages SET status='cancelled',error='Retired: owning queue job no longer exists',finished_at=now(),updated_at=now() WHERE group_id=ANY(%s) AND status IN ('pending','blocked')", (ids,))
            db.execute("UPDATE workflow_groups SET status='cancelled',error='Retired: owning queue job no longer exists',finished_at=now(),updated_at=now() WHERE group_id=ANY(%s)", (ids,))
            db.execute("UPDATE workflow_luws SET status='cancelled',current_step='retired',finished_at=now(),updated_at=now() WHERE group_id=ANY(%s) AND status IN ('planned','waiting','preflighted')", (ids,))
    return len(ids) + retired_luws


def reconcile_terminal_owners():
    """Close phantom unstarted stages using the exact terminal queue owner.

    A stage registered after its worker finished must not strand a workflow.
    Do not infer success from file existence or reopen failed media work.
    """
    from app.postgres_store import _lock_workflow_mutation
    repaired = 0
    with connection() as db:
        db.execute("SET LOCAL lock_timeout='3s'")
        db.execute("SET LOCAL statement_timeout='20s'")
        _lock_workflow_mutation(db)
        for table, kind in (('task_queue', 'q.task_type'), ('index_task_queue', "'index:'||q.job")):
            repaired += db.execute(f"""UPDATE workflow_stages s SET status=q.status,
                error=CASE WHEN q.status IN ('failed','cancelled') THEN q.error ELSE NULL END,
                finished_at=COALESCE(s.finished_at,now()),updated_at=now()
                FROM {table} q WHERE q.id::text=s.payload->>'task_id'
                AND {kind}=s.task_type
                AND replace(q.group_id,'-','')=replace(s.group_id::text,'-','')
                AND q.status IN ('succeeded','failed','cancelled') AND s.status IN ('pending','blocked')
                AND NOT EXISTS (SELECT 1 FROM workflow_locks k WHERE k.stage_id=s.stage_id)""").rowcount
    return repaired


def reconcile_terminal_groups():
    """Repair the group projection without reopening any completed work."""
    from app.postgres_store import _lock_workflow_mutation
    with connection() as db:
        db.execute("SET LOCAL lock_timeout='3s'")
        db.execute("SET LOCAL statement_timeout='20s'")
        _lock_workflow_mutation(db)
        rows = db.execute("""SELECT g.group_id,
            CASE WHEN EXISTS (SELECT 1 FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status='failed') THEN 'failed'
                 WHEN EXISTS (SELECT 1 FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status='succeeded') THEN 'succeeded'
                 ELSE 'cancelled' END AS resolved
            FROM workflow_groups g WHERE g.status IN ('pending','running','waiting','blocked')
            AND EXISTS (SELECT 1 FROM workflow_stages s WHERE s.group_id=g.group_id)
            AND NOT EXISTS (SELECT 1 FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status IN ('pending','running','blocked'))
            AND NOT EXISTS (SELECT 1 FROM task_queue q WHERE replace(q.group_id,'-','')=replace(g.group_id::text,'-','') AND q.status IN ('pending','running','failed'))
            AND NOT EXISTS (SELECT 1 FROM index_task_queue q WHERE replace(q.group_id,'-','')=replace(g.group_id::text,'-','') AND q.status IN ('pending','running','failed'))
            AND NOT EXISTS (SELECT 1 FROM workflow_luws l WHERE l.group_id=g.group_id AND l.status IN ('locked','applying','verifying'))
            AND NOT EXISTS (SELECT 1 FROM workflow_artifacts a WHERE a.group_id=g.group_id)
            AND NOT EXISTS (SELECT 1 FROM workflow_locks k WHERE k.group_id=g.group_id)
            LIMIT 500 FOR UPDATE OF g SKIP LOCKED""").fetchall()
        for row in rows:
            db.execute('UPDATE workflow_groups SET status=%s,finished_at=now(),updated_at=now() WHERE group_id=%s', (row['resolved'], row['group_id']))
    return len(rows)


def receipt(task_id, step):
    with connection() as db:
        row = db.execute('SELECT data FROM task_execution_receipts WHERE task_id=%s AND step=%s', (task_id, step)).fetchone()
    return row['data'] if row else None


def record(task_id, step, data):
    with connection() as db:
        db.execute("""INSERT INTO task_execution_receipts(task_id,step,data) VALUES(%s,%s,%s::jsonb)
            ON CONFLICT(task_id,step) DO UPDATE SET data=EXCLUDED.data,updated_at=now()""",
            (task_id, step, json.dumps(data)))


def stamp(path):
    path = Path(path)
    st = path.stat()
    return {'path': str(path.resolve()), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
            'ctime_ns': st.st_ctime_ns, 'device': st.st_dev, 'inode': st.st_ino}


@contextmanager
def task_context(task_id):
    previous = getattr(_local, 'task_id', None)
    _local.task_id = task_id
    try:
        yield
    finally:
        _local.task_id = previous


def replace_prepared(temporary, target, expected):
    """Record rename intent before touching the original; fsync before publish."""
    temporary, target = Path(temporary), Path(target)
    if stamp(target) != expected:
        raise RuntimeError('Media changed while preparing output; replacement refused')
    task_id = getattr(_local, 'task_id', None)
    output = stamp(temporary)
    output.pop('ctime_ns')  # rename changes ctime; capture it in the acknowledgement
    output['path'] = str(target.resolve())
    data = {'state': 'prepared', 'before': expected, 'after': output}
    with temporary.open('rb') as handle:
        os.fsync(handle.fileno())
    if task_id:
        record(task_id, 'file:' + str(target.resolve()), data)
    # Recheck after potentially slow database I/O, immediately before rename.
    if stamp(target) != expected:
        raise RuntimeError('Media changed before commit; replacement refused')
    os.replace(temporary, target)
    fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    committed = stamp(target)
    if any(committed.get(key) != value for key, value in output.items()):
        raise RuntimeError('Output changed before commit acknowledgement; review before retrying')
    if task_id:
        record(task_id, 'file:' + str(target.resolve()), {**data, 'after': committed, 'state': 'applied'})
    return committed


def inplace_checkpoint(path, before, *, applied=False):
    task_id = getattr(_local, 'task_id', None)
    if task_id:
        record(task_id, 'inplace:' + str(Path(path).resolve()),
               {'state': 'applied' if applied else 'prepared', 'before': before,
                'after': stamp(path) if applied else None})


def verify_output(saved):
    from app.v65 import media_configuration_signature
    expected = saved['signature']
    if not expected.get('digest') or media_configuration_signature(expected['path']) != expected:
        raise RuntimeError('Media changed after the recorded operation; recovery refused for safety')


def recovery(task_id, task_type, payload):
    """Return handler completion, or validate an interrupted subtitle replacement."""
    completed = receipt(task_id, 'handler')
    if completed:
        if completed.get('signature'):
            verify_output(completed)
        return completed, True
    mutation = (receipt(task_id, 'html') if task_type == 'subtitle_html_cleanup'
            else receipt(task_id, 'autofix') if task_type == 'subtitle_autofix' else None)
    if mutation:
        verify_output(mutation)
        return None, True
    with connection() as db:
        files = db.execute("SELECT data FROM task_execution_receipts WHERE task_id=%s AND (step LIKE 'file:%%' OR step LIKE 'inplace:%%')", (task_id,)).fetchall()
    changed = False
    for row in files:
        data = row['data']
        current = stamp((data.get('after') or data['before'])['path'])
        if data.get('after') and all(current.get(k) == value for k, value in data['after'].items()):
            changed = True
        elif current != data['before']:
            raise RuntimeError('Media changed after an interrupted commit; review recovery before retrying')
    if changed and task_type not in {'subtitle_html_cleanup', 'subtitle_autofix'}:
        raise RuntimeError('Output was committed before interruption; review recovery, do not repeat the edit')
    # HTML is idempotent. Autofix skips only source-bound confirmed file
    # receipts; it never reruns replacements against already corrected text.
    return None, changed


@contextmanager
def output_space(directory, required_bytes):
    """Serialize large writers on the same filesystem, including other processes.

    Lock survives neither process death nor reboot; no ghost reservation rows.
    Metadata-only edits do not use this gate. Free space is rechecked *after*
    admission, with the reserve left available after the estimated output.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    device = directory.stat().st_dev
    held = getattr(_local, 'devices', set())
    reserve = max(float(os.getenv('WORKFLOW_RESERVED_GB', '2')),
                  float(os.getenv('WORKFLOW_MIN_FREE_GB', '5'))) * 1024**3
    def check():
        if shutil.disk_usage(directory).free < max(0, required_bytes) + reserve:
            raise RuntimeError('Insufficient disk space for output plus safety reserve; no media changed')
    if device in held:
        check()
        yield
        return
    locks = Path(os.getenv('DATA_DIR', '/data')) / 'storage-locks'
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / f'{device}.lock').open('a') as handle:
        deadline = time.monotonic() + 3600
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError('Timed out waiting for safe disk write admission; retry later')
                time.sleep(0.25)
        _local.devices = held | {device}
        try:
            check()
            yield
        finally:
            _local.devices = held
            fcntl.flock(handle, fcntl.LOCK_UN)


class WriteCommandError(subprocess.CalledProcessError):
    """Keep the tool's actual diagnosis visible in queue errors and logs."""

    def __str__(self):
        detail = (self.stderr or '').strip()
        return super().__str__() + (f': {detail}' if detail else '')


def run_write_command(command, directory, timeout=3600, accepted_returncodes=(0,)):
    """Stop a growing remux before it consumes the protected free-space floor."""
    floor = max(float(os.getenv('WORKFLOW_RESERVED_GB', '2')),
                float(os.getenv('WORKFLOW_MIN_FREE_GB', '5'))) * 1024**3
    with tempfile.TemporaryFile() as errors:
        # mkvmerge writes warnings to stdout, unlike FFmpeg. Never discard them.
        process = subprocess.Popen(command, stdout=errors, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if shutil.disk_usage(directory).free < floor:
                    raise RuntimeError('Output stopped at disk safety reserve; original media retained')
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                if os.fstat(errors.fileno()).st_size > 16 * 1024**2:
                    raise RuntimeError('Output stopped: excessive tool diagnostics; original media retained')
                time.sleep(0.25)
            length = os.fstat(errors.fileno()).st_size
            errors.seek(max(0, length - 8000))
            message = errors.read().decode('utf-8', errors='replace')
            if process.returncode not in accepted_returncodes:
                raise WriteCommandError(process.returncode, command, stderr=message)
            return {'returncode': process.returncode, 'output': message, 'truncated': length > 8000}
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def copy_recovery(source, destination):
    """Bound copying to the admitted source size, preserve attrs and fsync."""
    source, destination = Path(source), Path(destination)
    before = stamp(source)
    remaining = before['size']
    floor = max(float(os.getenv('WORKFLOW_RESERVED_GB', '2')),
                float(os.getenv('WORKFLOW_MIN_FREE_GB', '5'))) * 1024**3
    with source.open('rb') as incoming, destination.open('wb') as outgoing:
        while remaining:
            if shutil.disk_usage(destination.parent).free < floor:
                raise RuntimeError('Recovery copy stopped at disk safety reserve')
            block = incoming.read(min(4 * 1024**2, remaining))
            if not block:
                raise RuntimeError('Source truncated during recovery copy')
            outgoing.write(block)
            remaining -= len(block)
        if incoming.read(1) or stamp(source) != before:
            raise RuntimeError('Source changed during recovery copy')
        outgoing.flush()
        os.fsync(outgoing.fileno())
    shutil.copystat(source, destination)
    with destination.open('rb') as saved:
        os.fsync(saved.fileno())
    # Publish both the file entry and a newly-created per-workflow directory
    # durably before the database is allowed to mark this backup complete.
    for directory in (destination.parent, destination.parent.parent):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
