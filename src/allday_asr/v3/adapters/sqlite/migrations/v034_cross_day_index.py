"""Additive bounded discussion projection and reversible decisions."""
SQL = """
CREATE TABLE cross_day_index_revisions(revision INTEGER PRIMARY KEY,scope_json TEXT NOT NULL,value_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE cross_day_link_decisions(source TEXT NOT NULL,target TEXT NOT NULL,action TEXT NOT NULL CHECK(action IN ('link','reject','detach')),PRIMARY KEY(source,target));
CREATE TRIGGER cross_day_index_no_update BEFORE UPDATE ON cross_day_index_revisions BEGIN SELECT RAISE(ABORT,'immutable memory revision'); END;
CREATE TRIGGER cross_day_index_no_delete BEFORE DELETE ON cross_day_index_revisions BEGIN SELECT RAISE(ABORT,'immutable memory revision'); END;
ALTER TABLE sync_cursors RENAME TO sync_cursors_v6;
CREATE TABLE sync_cursors(device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE RESTRICT,projection_version INTEGER NOT NULL CHECK(projection_version=7),acknowledged_sequence INTEGER NOT NULL CHECK(acknowledged_sequence>=0),updated_at TEXT NOT NULL);
INSERT INTO sync_cursors SELECT device_id,7,acknowledged_sequence,updated_at FROM sync_cursors_v6;
DROP TABLE sync_cursors_v6;
"""
