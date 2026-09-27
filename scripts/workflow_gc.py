"""Conservative workflow GC: inventory by default; terminal history only on opt-in.

Never removes media, artifact files, failed workflows or pending approval data.
Requires an existing verified backup path for --apply-history. Active claims and
ownership are rechecked in the same locked transaction as each deletion batch.
"""
import argparse
import json
from pathlib import Path
from app.postgres_store import connection, _lock_workflow_mutation

ELIGIBLE = """g.status IN ('succeeded','cancelled')
 AND NOT EXISTS (SELECT 1 FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status NOT IN ('succeeded','cancelled'))
 AND NOT EXISTS (SELECT 1 FROM workflow_artifacts a WHERE a.group_id=g.group_id)
 AND NOT EXISTS (SELECT 1 FROM workflow_locks l WHERE l.group_id=g.group_id)
 AND NOT EXISTS (SELECT 1 FROM workflow_luws l WHERE l.group_id=g.group_id AND l.status NOT IN ('committed','rolled_back','cancelled','obsolete'))
 AND NOT EXISTS (SELECT 1 FROM task_queue q WHERE replace(q.group_id,'-','')=replace(g.group_id::text,'-',''))
 AND NOT EXISTS (SELECT 1 FROM index_task_queue q WHERE replace(q.group_id,'-','')=replace(g.group_id::text,'-',''))
 AND g.group_id NOT IN (SELECT group_id FROM workflow_groups WHERE status IN ('succeeded','cancelled') ORDER BY updated_at DESC,group_id LIMIT 2000)"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply-history', action='store_true')
    parser.add_argument('--verified-backup')
    args = parser.parse_args()
    if args.apply_history and (not args.verified_backup or not Path(args.verified_backup).is_file()):
        parser.error('A verified backup file is required')
    with connection() as db:
        db.execute('SET TRANSACTION READ ONLY')
        db.execute("SET LOCAL statement_timeout='20s'")
        count = db.execute('SELECT count(*) AS n FROM workflow_groups g WHERE '+ELIGIBLE).fetchone()['n']
        artifacts = [dict(row) for row in db.execute('SELECT g.status,count(*) AS files FROM workflow_artifacts a JOIN workflow_groups g USING(group_id) GROUP BY g.status')]
    print(json.dumps({'eligible_terminal_groups': count, 'protected_artifacts': artifacts}), flush=True)
    if not args.apply_history: return
    removed = 0
    while True:
        with connection() as db:
            db.execute("SET LOCAL lock_timeout='5s'")
            db.execute("SET LOCAL statement_timeout='20s'")
            _lock_workflow_mutation(db)
            rows = db.execute('SELECT g.group_id FROM workflow_groups g WHERE '+ELIGIBLE+' LIMIT 500 FOR UPDATE OF g SKIP LOCKED').fetchall()
            if not rows: break
            ids = [row['group_id'] for row in rows]
            deleted = db.execute('DELETE FROM workflow_groups g WHERE g.group_id=ANY(%s::uuid[]) AND '+ELIGIBLE, (ids,)).rowcount
            removed += deleted
        print(json.dumps({'removed_terminal_groups': removed}), flush=True)
        if not deleted: break
    print(json.dumps({'complete': True, 'removed_terminal_groups': removed, 'artifact_files_removed': 0}), flush=True)


if __name__ == '__main__': main()
