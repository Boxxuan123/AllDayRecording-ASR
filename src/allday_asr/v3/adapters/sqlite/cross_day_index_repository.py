import json
from allday_asr.v3.domain.memory_content import safe_memory


class CrossDayIndexRepository:
    def __init__(self, connection):
        self.connection = connection

    def current(self):
        row = self.connection.execute(
            "SELECT * FROM cross_day_index_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        return (
            (safe_memory(json.loads(row["scope_json"])), safe_memory(json.loads(row["value_json"])))
            if row
            else (None, None)
        )

    def has_batch(self, batch_id):
        return bool(self.connection.execute("SELECT 1 FROM cross_day_index_revisions WHERE json_extract(scope_json,'$._batch_id')=? LIMIT 1",(batch_id,)).fetchone())

    def history(self):
        # Existing immutable revisions are the history owner; no parallel queue/table.
        rows=self.connection.execute("SELECT scope_json,value_json FROM cross_day_index_revisions WHERE revision IN (SELECT MAX(revision) FROM cross_day_index_revisions GROUP BY json_extract(scope_json,'$.first'),json_extract(scope_json,'$.last'),json_extract(scope_json,'$.contacts')) ORDER BY revision DESC").fetchall()
        result=[]
        for row in rows:
            scope=safe_memory(json.loads(row[0]));value=safe_memory(json.loads(row[1]))
            scope={k:v for k,v in scope.items() if k not in {'_history_scopes','_archive'}}
            value={k:v for k,v in value.items() if not k.startswith('_')}
            ids={r["id"] for r in scope["manifest"]}
            value["discussions"]=[r for r in value["discussions"] if r["id"] in ids]
            result.append([scope,value])
        return result

    def decisions(self):
        return [
            dict(r)
            for r in self.connection.execute(
                "SELECT * FROM cross_day_link_decisions ORDER BY source,target"
            )
        ]

    def decide(self, source, target, action):
        if action in {"link", "detach"}:
            self.connection.execute(
                "DELETE FROM cross_day_link_decisions WHERE source=? AND action IN ('link','detach')",
                (source,),
            )
        self.connection.execute(
            "INSERT INTO cross_day_link_decisions VALUES(?,?,?) ON CONFLICT(source,target) DO UPDATE SET action=excluded.action",
            (source, target, action),
        )

    def save(self, scope, value, now):
        self.connection.execute(
            "INSERT INTO cross_day_index_revisions VALUES(?,?,?,?)",
            (
                value["revision"],
                json.dumps(safe_memory(scope), ensure_ascii=False, sort_keys=True),
                json.dumps(safe_memory(value), ensure_ascii=False, sort_keys=True),
                now,
            ),
        )


def memory_review_items(connection):
    _, value = CrossDayIndexRepository(connection).current()
    if not value:
        return []
    from allday_asr.v3.domain.ids import stable_ulid

    rows = {r["id"]: r for r in value["discussions"]}
    result = []
    for candidate in value["candidates"]:
        source, target = candidate["source"], candidate["target"]
        result.append(
            {
                "review_id": "memory-link:"
                + stable_ulid("memory-link", source, target),
                "kind": "person_memory",
                "priority": "normal",
                "source_id": source,
                "source_revision": value["revision"],
                "session_id": None,
                "person_id": None,
                "title": "是否为同一件具体事情？",
                "summary": rows[source]["title"] + " ↔ " + rows[target]["title"],
                "reason": "memory_association_requires_confirmation",
                "evidence_count": 2,
                "created_at": value["updated_at"],
                "updated_at": value["updated_at"],
                "context": {
                    "memory_association": {**candidate, "revision": value["revision"]},
                    "source_text": "\n".join(
                        f"{e.get('speaker','未知发送者')} · {e['occurred_at']} · is_from_self={e.get('is_from_self',e.get('identity_self'))} · {e.get('identity_basis','来源身份依据')} · {e.get('role','core')}：{e['quote']}"
                        for key in [source, target]
                        for e in rows[key]["evidence"][:8]
                    ),
                },
            }
        )
    return result
