"""Remove only reviewed obsolete schema objects, with transactional guards.

Dry-run by default. Run with PYTHONPATH=/app and --apply after deploying source
that no longer creates these objects. Never uses CASCADE or deletes media data.
"""
import argparse
import json
from psycopg import sql
from app.postgres_store import connection


def cleanup(db, apply=False, schema='public'):
    actions = []
    db.execute("SET LOCAL lock_timeout='3s'")
    db.execute("SET LOCAL statement_timeout='20s'")
    def exists(table, column=None):
        if column:
            return db.execute('SELECT 1 FROM information_schema.columns WHERE table_schema=%s AND table_name=%s AND column_name=%s', (schema,table,column)).fetchone()
        return db.execute('SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s', (schema,table)).fetchone()
    for table in ('performance_metric','tv_stream_index_settings','feature_migrations'):
        if not exists(table): continue
        target=sql.Identifier(schema,table)
        if apply: db.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(target))
        if table=='performance_metric':
            unexpected=db.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(target)).fetchone()
        elif table=='tv_stream_index_settings':
            # This retired projection stores only its version marker, not user
            # settings. Refuse removal if any unrecognized data is present.
            unexpected=db.execute(sql.SQL("SELECT 1 FROM {} WHERE key <> 'format_version' OR value <> '2' LIMIT 1").format(target)).fetchone()
        else:
            retired = ['canonical_index_design_v3','legacy_projection_schema_removed_v1',
                'on_demand_indexes_v1','plex_aware_parser_v3','plex_language_semantics_v4',
                'preview_anchor_5min_v1','preview_cache_policy_v2','preview_cache_removed_v1',
                'subtitle_extended_index_v1','subtitle_preview_cache_removed_v1',
                'unified_stream_index_parser_v2','unified_stream_index_v1']
            unexpected=db.execute(sql.SQL('SELECT 1 FROM {} WHERE NOT (name=ANY(%s)) LIMIT 1').format(target),(retired,)).fetchone()
        if unexpected: raise RuntimeError(f'Refusing to remove unexpected data in {table}')
        actions.append({'drop_table':table})
        if apply: db.execute(sql.SQL('DROP TABLE {}').format(target))
    table='core_index_parity_runs'
    if exists(table,'cursor_cursor_offset'):
        if not exists(table,'cursor_offset'): raise RuntimeError('Canonical cursor column is missing')
        target=sql.Identifier(schema,table)
        if apply: db.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(target))
        if db.execute(sql.SQL('SELECT 1 FROM {} WHERE cursor_cursor_offset IS DISTINCT FROM 0 LIMIT 1').format(target)).fetchone():
            raise RuntimeError('Obsolete cursor column contains data; manual reconciliation required')
        actions.append({'drop_column':table+'.cursor_cursor_offset'})
        if apply: db.execute(sql.SQL('ALTER TABLE {} DROP COLUMN cursor_cursor_offset').format(target))
    for obsolete, retained in [('media_video_title_path','media_video_title_pkey'),('track_name_correction_language_lookup','track_name_correction_lookup')]:
        rows=db.execute('''SELECT c.relname, i.indisvalid, i.indisunique,
            pg_get_indexdef(c.oid) AS definition FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_index i ON i.indexrelid=c.oid
            WHERE n.nspname=%s AND c.relname IN (%s,%s)''',(schema,obsolete,retained)).fetchall()
        indexes={r['relname']:r for r in rows}
        if obsolete not in indexes: continue
        old=indexes[obsolete]; keep=indexes.get(retained)
        if not keep or not keep['indisvalid'] or old['indisunique'] or old['definition'].split(' ON ',1)[1] != keep['definition'].split(' ON ',1)[1]:
            raise RuntimeError(f'Index equivalence check failed for {obsolete}')
        actions.append({'drop_duplicate_index':obsolete,'retained':retained})
        if apply: db.execute(sql.SQL('DROP INDEX {}').format(sql.Identifier(schema,obsolete)))
    return actions


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    with connection() as db:
        actions=cleanup(db,args.apply)
    print(json.dumps({'applied':args.apply,'actions':actions}))
