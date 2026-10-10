"""PC-private chat provenance; events remain the shared task aggregate."""

SQL = """
CREATE TABLE chat_followup_sources (
 source_key TEXT PRIMARY KEY, event_id TEXT NOT NULL,
 dataset TEXT NOT NULL, conversation_key TEXT NOT NULL,
 latest_sent_at INTEGER, human_override INTEGER NOT NULL DEFAULT 0,
 ignored INTEGER NOT NULL DEFAULT 0, conflict_json TEXT,
 FOREIGN KEY(event_id) REFERENCES event_current_states(event_id)
);
CREATE INDEX chat_followup_event ON chat_followup_sources(event_id);
CREATE TABLE chat_followup_effects (
 effect_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, details_json TEXT NOT NULL,
 FOREIGN KEY(event_id) REFERENCES event_current_states(event_id)
);
CREATE TABLE chat_followup_jobs (
 job_id TEXT PRIMARY KEY, state TEXT NOT NULL, value_json TEXT NOT NULL,
 updated_at REAL NOT NULL
);
"""


def generalize_event_sessions(connection):
    # Runner disables FK enforcement BEFORE BEGIN only for this migration;
    # validates all FKs before commit. No fake recording sessions are inserted.
    tables = ("event_operations", "event_current_states")
    ddl = {}
    extras = []
    for table in tables:
        ddl[table] = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        extras.extend(
            r[0]
            for r in connection.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
                (table,),
            )
        )
        sql = ddl[table].replace(
            "CREATE TABLE " + table, "CREATE TABLE chat_new_" + table, 1
        )
        sql = sql.replace("session_id TEXT NOT NULL", "session_id TEXT")
        connection.execute(sql)
        connection.execute(f"INSERT INTO chat_new_{table} SELECT * FROM {table}")
    for table in reversed(tables):
        connection.execute(f"DROP TABLE {table}")
    for table in tables:
        connection.execute(f"ALTER TABLE chat_new_{table} RENAME TO {table}")
    for sql in extras:
        connection.execute(sql)
