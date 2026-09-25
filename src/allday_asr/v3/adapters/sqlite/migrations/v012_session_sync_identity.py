"""Republish lost Phone identities without deleting recordings or old events."""

SQL = """
INSERT INTO change_events (
    resource_type, resource_id, revision, operation, payload_json, created_at
)
SELECT e.resource_type, e.resource_id, e.revision, e.operation,
       json_set(e.payload_json, '$.session_key', json_extract(m.entries_json, '$.sessionKey')),
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM change_events e
JOIN session_manifests m ON m.session_id = e.resource_id
JOIN recording_sessions s ON s.session_id = e.resource_id
WHERE e.resource_type = 'recording_session' AND e.operation = 'upsert'
  AND s.tombstoned_at IS NULL
  AND e.sequence = (SELECT MAX(c.sequence) FROM change_events c
                    WHERE c.resource_type = e.resource_type AND c.resource_id = e.resource_id)
  AND json_type(m.entries_json, '$.sessionKey') = 'text'
  AND json_extract(m.entries_json, '$.sessionKey') != ''
  AND COALESCE(json_extract(e.payload_json, '$.session_key'), '') !=
      json_extract(m.entries_json, '$.sessionKey');
"""
