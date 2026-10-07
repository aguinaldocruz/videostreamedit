"""Fault injection against disposable files and a PostgreSQL rollback-only schema."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import os
import tempfile
import uuid
import sys

from app import postgres_store as store, job_safety as safety

with store.connection() as db, tempfile.TemporaryDirectory() as folder:
    schema = 'safety_test_' + uuid.uuid4().hex
    db.execute(f'CREATE SCHEMA {schema}')
    db.execute(f'SET LOCAL search_path TO {schema}')
    @contextmanager
    def same_connection():
        yield db
    with patch.object(store, 'connection', same_connection), patch.object(safety, 'connection', same_connection):
        store.initialize_schema()
        db.execute('CREATE TABLE task_queue(id bigint primary key,group_id text,task_type text,status text)')
        db.execute('CREATE TABLE index_task_queue(id bigint,group_id text,job text,status text,path text,expedite_until text)')
        db.execute('CREATE TABLE plex_internal_change_scope(expected_size integer,expected_modified integer,created_at integer,expires_at integer)')
        db.execute('CREATE TABLE ocr_staged_backups(size_bytes integer)')
        safety.initialize()
        safety.initialize()  # Startup migration is repeatable.
        db.execute('INSERT INTO plex_internal_change_scope VALUES(6000000000,2200000000,2200000000,2200600000)')
        db.execute('INSERT INTO ocr_staged_backups VALUES(6000000000)')
        db.execute("INSERT INTO task_queue VALUES(1,NULL,'subtitle_html_cleanup','failed')")
        path = Path(folder)/'media.mkv'; path.write_bytes(b'original')
        prepared = Path(folder)/'prepared.mkv'; prepared.write_bytes(b'replacement')
        before = safety.stamp(path)
        with safety.task_context(1):
            safety.replace_prepared(prepared, path, before)
        saved = safety.receipt(1, 'file:' + str(path.resolve()))
        assert saved['state'] == 'applied' and path.read_bytes() == b'replacement'
        assert safety.recovery(1, 'subtitle_html_cleanup', {}) == (None, True)
        assert safety.recovery(1, 'subtitle_autofix', {}) == (None, True)
        try:
            safety.recovery(1, 'media_edit', {})
        except RuntimeError as exc:
            assert 'committed' in str(exc)
        else:
            raise AssertionError('Uncertain committed edits must not repeat')
        path.write_bytes(b'new external change')
        try:
            safety.recovery(1, 'subtitle_html_cleanup', {})
        except RuntimeError:
            pass
        else:
            raise AssertionError('New external change must prevent recovery')
        prepared.write_bytes(b'do not install')
        try:
            safety.replace_prepared(prepared, path, before)
        except RuntimeError:
            assert path.read_bytes() == b'new external change'
        else:
            raise AssertionError('Concurrent edit overwritten')

        # Rename succeeded but the acknowledgement failed (power/DB loss).
        before = safety.stamp(path)
        original_record = safety.record
        def fail_ack(task, step, data):
            if data['state'] == 'applied':
                raise RuntimeError('simulated database outage after rename')
            original_record(task, step, data)
        with safety.task_context(1), patch.object(safety, 'record', fail_ack):
            try:
                safety.replace_prepared(prepared, path, before)
            except RuntimeError:
                pass
        assert safety.receipt(1, 'file:' + str(path.resolve()))['state'] == 'prepared'
        assert safety.recovery(1, 'subtitle_html_cleanup', {}) == (None, True)

        # Missing queue owners are retired; recovery owners are preserved.
        for protected in (False, True):
            gid = uuid.uuid4()
            db.execute("INSERT INTO workflow_groups(group_id,kind,status,updated_at) VALUES(%s,'fixture','pending',now()-interval '1 hour')", (gid,))
            db.execute("INSERT INTO workflow_stages(group_id,stage_number,task_type,status) VALUES(%s,1,'media_edit','pending')", (gid,))
            if protected:
                db.execute("INSERT INTO workflow_artifacts(group_id,artifact_path,kind) VALUES(%s,'/fixture','media-original')", (gid,))
            safety.reconcile_orphans()
            status = db.execute('SELECT status FROM workflow_groups WHERE group_id=%s', (gid,)).fetchone()['status']
            assert status == ('pending' if protected else 'cancelled')

        # Exercise the real HTML handler: failure after cleanup must not remux again.
        from app import v68, v65, v80
        signature = {'path': str(path), 'digest': 'fixture'}
        calls = []
        def cleanup(*args, **kwargs):
            calls.append('cleanup')
            return {'changed': True, 'path': str(path)}
        with patch.object(v68, 'apply_subtitle_cleanups', cleanup), patch.object(v65, 'update_progress'), patch.object(v65, 'media_configuration_signature', return_value=signature), patch.object(v80, 'request_media_indexes'), patch.object(v68, 'register_internal_change_scope', side_effect=RuntimeError('bookkeeping outage')):
            try:
                v68.process_subtitle_html(1, {'path': str(path), 'cleanups': [{'type_index': 0}]})
            except RuntimeError:
                pass
        with patch.object(v68, 'apply_subtitle_cleanups', cleanup), patch.object(v65, 'update_progress'), patch.object(v65, 'media_configuration_signature', return_value=signature), patch.object(v80, 'request_media_indexes'), patch.object(v68, 'register_internal_change_scope'):
            assert v68.process_subtitle_html(1, {'path': str(path), 'cleanups': [{'type_index': 0}]})['cleaned'] == 1
        assert calls == ['cleanup']
        # Verified HTML replacement needs one output, not an extra full-media
        # workflow snapshot. Other edit types retain their recovery strategy.
        with patch.object(store, '_stage_root', side_effect=AssertionError('Unexpected full-size HTML staging')):
            store.prepare_task_artifact(str(uuid.uuid4()), 1, 'subtitle_html_cleanup', str(path), {})
            store.prepare_task_artifact(str(uuid.uuid4()), 1, 'subtitle_autofix', str(path), {})
        # Completed index stages must not leave their group marked pending.
        gid = uuid.uuid4()
        db.execute("INSERT INTO workflow_groups(group_id,kind,status) VALUES(%s,'index:core','pending')", (gid,))
        db.execute("INSERT INTO workflow_stages(group_id,stage_number,task_type,status) VALUES(%s,1,'index:core','succeeded')", (gid,))
        db.execute("INSERT INTO index_task_queue(id,group_id,job,status) VALUES(44,%s,'core','succeeded')", (gid.hex,))
        assert safety.reconcile_terminal_groups() >= 1
        assert db.execute('SELECT status FROM workflow_groups WHERE group_id=%s', (gid,)).fetchone()['status'] == 'succeeded'
        from app.pg_compat import Connection
        adapter = object.__new__(Connection); adapter.raw = db; adapter.total_changes = 0
        @contextmanager
        def adapted_connection():
            yield adapter
        from app import subtitle_cache as cache
        with patch.object(cache, 'connect', adapted_connection):
            cache.ensure_subtitle_cache_schema()
            item = cache.TextSubtitle('embedded', 0, '', 'subrip', '1\n00:00:00,000 --> 00:00:01,000\nHello\n')
            cache.enqueue_media(str(path))
            revision = cache.pending_revision(str(path))
            cache.publish_replacement_cache(str(path), 'fresh', {item.key}, [item], image_before_signature='',
                                            image_after_signature='', expected_images=set())
            assert cache.reconcile_source_cache(str(path), 'fresh', {item.key: item.codec}, '', set(), revision)
            assert cache.get_valid_media(str(path), 'fresh') == [item]
            assert cache.pending_revision(str(path)) is None
            assert not cache.reconcile_source_cache(str(path), 'changed', {item.key: item.codec}, '', set(), None)
            assert cache.get_valid_tracks(str(path), 'changed') == [] and cache.pending_revision(str(path)) == 1
        db.execute('CREATE TABLE task_queue_expedite(task_id bigint,expires_at timestamptz)')
        db.execute("INSERT INTO task_queue_expedite VALUES(1,now()+interval '1 hour')")
        db.execute("INSERT INTO index_task_queue(id,job,status,path,expedite_until) VALUES(45,'core','pending','/fixture','2000-01-01T00:00:00+00:00')")
        with patch.object(v80, 'connection', adapted_connection), patch.object(v65, '_active_expedite_expiry', return_value='2099-01-01T00:00:00+00:00'):
            assert v80.inherit_existing_index_expedites() == 1
        assert db.execute('SELECT expedite_until FROM index_task_queue WHERE id=45').fetchone()['expedite_until'].startswith('2099')

        # Rollback checks both the saved bytes and the failed output identity.
        gid = uuid.uuid4()
        db.execute("INSERT INTO workflow_groups(group_id,kind,status) VALUES(%s,'fixture','failed')", (gid,))
        db.execute('UPDATE task_queue SET group_id=%s WHERE id=1', (gid.hex,))
        backup = Path(folder)/'original.saved'; backup.write_bytes(b'safe original')
        prepared.write_bytes(b'failed output')
        with safety.task_context(1):
            safety.replace_prepared(prepared, path, safety.stamp(path))
        db.execute("INSERT INTO workflow_artifacts(group_id,original_path,artifact_path,kind,checksum) VALUES(%s,%s,%s,'media-original',%s)", (gid, str(path), str(backup), store._file_digest(backup)))
        with patch.dict(os.environ, {'DATA_DIR': folder, 'WORKFLOW_MIN_FREE_GB': '0', 'WORKFLOW_RESERVED_GB': '0'}):
            # A competing LUW must block rollback.
            other = store.create_luw(str(path), 'test-edit', 'queued')
            assert store.acquire_luw_lock(other, str(path))
            try:
                store.rollback_workflow(str(gid))
            except RuntimeError as exc:
                assert 'in use' in str(exc)
            else:
                raise AssertionError('Rollback ignored competing media lock')
            store.release_luw_lock(other, str(path))
            store.transition_luw(other, 'cancelled')
            path.write_bytes(b'newer external edit')
            try:
                store.rollback_workflow(str(gid))
            except RuntimeError as exc:
                assert 'changed' in str(exc)
                assert path.read_bytes() == b'newer external edit'
            else:
                raise AssertionError('Rollback overwrote a newer edit')
            prepared.write_bytes(b'failed output')
            with safety.task_context(1):
                safety.replace_prepared(prepared, path, safety.stamp(path))
            assert store.rollback_workflow(str(gid))['restored'] == 1
            assert path.read_bytes() == b'safe original'
        # Explicit retry also reopens a failed LUW, not only its queue stage.
        uid = store.create_luw(str(path), 'media_edit', 'queued', group_id=str(gid), idempotency_key='task:1')
        store.transition_luw(uid, 'failed', error='fixture')
        store.reset_task_stage_for_retry(str(gid), 1, db=db)
        assert db.execute('SELECT status FROM workflow_luws WHERE luw_id=%s', (uid,)).fetchone()['status'] == 'planned'

        gid = uuid.uuid4()
        db.execute("INSERT INTO workflow_groups(group_id,kind,status) VALUES(%s,'fixture','running')", (gid,))
        db.execute("INSERT INTO workflow_stages(group_id,stage_number,task_type,status,payload) VALUES(%s,1,'media_edit','running','{\"task_id\":2}')", (gid,))
        def interrupted_copy(source, destination):
            destination.write_bytes(b'partial')
            raise OSError('simulated power loss')
        with patch.object(store, '_stage_root', return_value=Path(folder)), patch.dict(os.environ, {'DATA_DIR': folder, 'WORKFLOW_MIN_FREE_GB': '0', 'WORKFLOW_RESERVED_GB': '0'}):
            with patch.object(safety, 'copy_recovery', interrupted_copy):
                try:
                    store.prepare_task_artifact(str(gid), 2, 'media_edit', str(path), {})
                except OSError:
                    pass
            assert db.execute('SELECT status FROM workflow_artifacts WHERE group_id=%s', (gid,)).fetchone()['status'] == 'copying'
            store.prepare_task_artifact(str(gid), 2, 'media_edit', str(path), {})
            artifact = db.execute('SELECT * FROM workflow_artifacts WHERE group_id=%s', (gid,)).fetchone()
            assert artifact['status'] == 'owned' and Path(artifact['artifact_path']).read_bytes() == path.read_bytes()
    db.rollback()

    with patch.dict(os.environ, {'DATA_DIR': folder, 'WORKFLOW_MIN_FREE_GB': '0', 'WORKFLOW_RESERVED_GB': '0'}):
        with patch.object(safety.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 5})()):
            try:
                with safety.output_space(folder, 6):
                    raise AssertionError('Write was admitted without space')
            except RuntimeError:
                pass
        with safety.output_space(folder, 1):
            with safety.output_space(folder, 1):  # same-thread nesting does not deadlock
                pass
        with patch.object(safety.shutil, 'disk_usage', return_value=type('Usage', (), {'free': -1})()):
            try:
                safety.run_write_command([sys.executable, '-c', 'import time; time.sleep(60)'], folder)
            except RuntimeError as exc:
                assert 'reserve' in str(exc)
            else:
                raise AssertionError('Growing output was not stopped at the disk reserve')

print('PASS: >2 GiB/2038 values, interrupted commit, stale output rejection, orphan ownership, HTML bookkeeping-only retry, disk admission')
