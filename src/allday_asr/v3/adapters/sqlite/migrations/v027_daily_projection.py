"""Accept V3.8 projection cursors without resetting acknowledged progress."""
SQL = """
ALTER TABLE sync_cursors RENAME TO sync_cursors_v4;
CREATE TABLE sync_cursors (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE RESTRICT,
    projection_version INTEGER NOT NULL CHECK(projection_version = 5),
    acknowledged_sequence INTEGER NOT NULL CHECK(acknowledged_sequence >= 0),
    updated_at TEXT NOT NULL
);
INSERT INTO sync_cursors(device_id,projection_version,acknowledged_sequence,updated_at)
SELECT device_id,5,acknowledged_sequence,updated_at FROM sync_cursors_v4;
DROP TABLE sync_cursors_v4;
"""
