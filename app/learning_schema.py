"""One owner for learned track-name schema; upgrades never discard learning."""


DDL = """CREATE TABLE {table} (
    stream_type TEXT NOT NULL CHECK(stream_type IN ('audio','subtitle')),
    track_language TEXT NOT NULL DEFAULT '', old_value TEXT NOT NULL, new_value TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 1, last_used TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(stream_type,track_language,old_value,new_value))"""


def ensure_learning_schema(db):
    db.execute(DDL.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ').format(table='track_name_correction_history'))
    cursor = db.execute('SELECT * FROM track_name_correction_history LIMIT 0')
    description = cursor.cursor.description if hasattr(cursor, 'cursor') else cursor.description
    columns = {column[0] for column in description}
    if 'track_language' not in columns:
        # The transaction preserves the old table if any copy/DDL check fails.
        # Unknown historical language stays blank; never invent a language.
        db.execute(DDL.format(table='track_name_correction_history_upgrade'))
        enabled = 'enabled' if 'enabled' in columns else '1'
        db.execute('''INSERT INTO track_name_correction_history_upgrade
            (stream_type,track_language,old_value,new_value,use_count,last_used,enabled)
            SELECT stream_type,'',old_value,new_value,use_count,last_used,'''+enabled+''' FROM track_name_correction_history''')
        before = db.execute('SELECT count(*) FROM track_name_correction_history').fetchone()[0]
        after = db.execute('SELECT count(*) FROM track_name_correction_history_upgrade').fetchone()[0]
        if before != after:
            raise RuntimeError('Learned-value upgrade verification failed; transaction must roll back')
        db.execute('DROP TABLE track_name_correction_history')
        db.execute('ALTER TABLE track_name_correction_history_upgrade RENAME TO track_name_correction_history')
    elif 'enabled' not in columns:
        db.execute('ALTER TABLE track_name_correction_history ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
    db.execute('''CREATE INDEX IF NOT EXISTS track_name_correction_lookup
        ON track_name_correction_history(stream_type,track_language,old_value,use_count DESC)''')
