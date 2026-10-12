"""Security-only rewrite of derived copies; originals and human decisions untouched."""
SQL = """
DROP TRIGGER cross_day_index_no_update;
DROP TRIGGER protect_change_events_from_update;
UPDATE cross_day_index_revisions SET scope_json=memory_safe_json(scope_json),value_json=memory_safe_json(value_json);
UPDATE change_events SET payload_json=memory_safe_json(payload_json) WHERE resource_type='memory_index' AND payload_json IS NOT NULL;
CREATE TRIGGER cross_day_index_no_update BEFORE UPDATE ON cross_day_index_revisions BEGIN SELECT RAISE(ABORT,'immutable memory revision'); END;
CREATE TRIGGER protect_change_events_from_update BEFORE UPDATE ON change_events BEGIN SELECT RAISE(ABORT,'change_events is append-only'); END;
"""
