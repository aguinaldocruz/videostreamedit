"""Read-only PostgreSQL schema/performance inventory. Prints JSON, no changes.

Run with PYTHONPATH=/app python /app/scripts/schema_audit.py. Do not interpret
zero scans or empty tables as permission to remove optional/recovery structures.
"""
import json
from app.postgres_store import connection


def audit():
    with connection() as db:
        db.execute('SET TRANSACTION READ ONLY')
        db.execute("SET LOCAL statement_timeout='15s'")
        queries = {
            'tables': "SELECT relname,n_live_tup,n_dead_tup,pg_total_relation_size(relid) AS bytes,last_autovacuum,last_autoanalyze FROM pg_stat_user_tables ORDER BY relname",
            'columns': "SELECT table_name,column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position",
            'constraints': "SELECT conrelid::regclass::text AS table_name,conname,contype,convalidated,pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE connamespace='public'::regnamespace ORDER BY conrelid::regclass::text,conname",
            'indexes': "SELECT s.relname,s.indexrelname,s.idx_scan,pg_relation_size(s.indexrelid) AS bytes,i.indisvalid,i.indisunique,pg_get_indexdef(s.indexrelid) AS definition FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid=s.indexrelid ORDER BY s.relname,s.indexrelname",
            'activity': "SELECT state,wait_event_type,count(*) AS connections FROM pg_stat_activity WHERE datname=current_database() GROUP BY state,wait_event_type",
        }
        return {name: [dict(row) for row in db.execute(sql).fetchall()] for name, sql in queries.items()}


if __name__ == '__main__':
    print(json.dumps(audit(), default=str, indent=2))
