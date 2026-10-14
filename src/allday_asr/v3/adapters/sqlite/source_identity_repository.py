"""Reversible mappings in the existing chat cache; never writes voice or Core facts."""

import json
import time
from allday_asr.v3.domain.chat_data import packed, scope_key
from allday_asr.v3.domain.source_identity import identity_key, resolve_chat
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_identity_operations(
 operation_id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL,
 actor TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_identity_decisions(
 identity_key TEXT NOT NULL, person_id TEXT NOT NULL, action TEXT NOT NULL,
 basis TEXT NOT NULL, operation_id TEXT NOT NULL, PRIMARY KEY(identity_key,person_id));
"""


class SourceIdentities:
    def __init__(self, database, chat_path):
        self.database, self.chat_path = database, chat_path
        # Existing read helpers without startup migrations, observations or job recovery.
        self.cache = ChatCache.__new__(ChatCache)
        self.cache.path = chat_path

    def people(self):
        with self.database.read() as c:
            c.execute("PRAGMA busy_timeout=1000")
            rows = c.execute(
                "SELECT p.person_id,p.display_name,p.kind,coalesce(profile.aliases_json,'[]') AS aliases_json "
                "FROM persons p LEFT JOIN person_profile_revisions profile ON profile.person_id=p.person_id "
                "AND profile.revision=(SELECT max(revision) FROM person_profile_revisions WHERE person_id=p.person_id) "
                "WHERE p.user_confirmed=1 ORDER BY p.person_id LIMIT 501"
            ).fetchall()
        if len(rows) > 500:
            raise ValueError("人物目录超过本次 500 人上限")
        result = {}
        for row in rows:
            person = dict(row)
            aliases = json.loads(person.pop("aliases_json"))
            person["aliases"] = [a for a in aliases if isinstance(a, str) and a.strip()]
            result[person["person_id"]] = person
        return result

    def manual(self):
        if not self.chat_path.exists():
            return {}
        with self.cache.connect() as c:
            return {
                packed([r[0], r[1], r[2]]): r[3]
                for r in c.execute(
                    "SELECT platform,source,account,person_id FROM user_account_links"
                )
            }

    def recording_identities(self, offset=0):
        from .recall_sources import ANNOTATED, CANONICAL

        people = self.people()
        with self.database.read() as c:
            deadline = time.monotonic() + 3
            c.execute("PRAGMA busy_timeout=1000")
            c.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = c.execute(
                "SELECT u.session_id,u.speaker_track_id,"
                + ANNOTATED
                + " AS person_id FROM utterances u JOIN recording_sessions s ON s.session_id=u.session_id WHERE s.tombstoned_at IS NULL AND u.status='active' AND "
                + CANONICAL
                + " AND ("
                + ANNOTATED
                + ") IS NOT NULL GROUP BY u.session_id,u.speaker_track_id,person_id ORDER BY u.session_id,u.speaker_track_id,person_id LIMIT 51 OFFSET ?",
                (offset,),
            ).fetchall()
        return [
            {
                "key": "recording:" + r[0] + ":" + (r[1] or ""),
                "platform": "recording",
                "source": r[0],
                "account": r[1] or "",
                "label": people[r[2]]["display_name"],
                "person_id": r[2],
                "basis": "原转写人工确认；多个已确认录音来源复用同一人物",
                "decisions": [],
                "editable": False,
            }
            for r in rows[:50]
            if r[2] in people
        ], len(rows) > 50

    def version(self):
        return scope_key(
            {
                "links": self.manual(),
                "people": self.people(),
            }
        )

    def catalog(self, offset=0, person_id=""):
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("身份目录页码无效")
        people, manual = self.people(), self.manual()
        items, more = [], False
        if self.chat_path.exists():
            with self.cache.connect() as c:
                deadline = time.monotonic() + 3
                c.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                rows = c.execute(
                    "SELECT platform,source,sender,max(sent_at) AS latest FROM search_index WHERE dataset=? AND sender IS NOT NULL AND sender!='' GROUP BY platform,source,sender ORDER BY platform,source,sender LIMIT 51 OFFSET ?",
                    (self.cache.get("dataset", ""), offset),
                ).fetchall()
                more = len(rows) > 50
                tables = {
                    r[0]
                    for r in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for row in rows[:50]:
                    rec = json.loads(
                        c.execute(
                            "SELECT body FROM search_index WHERE dataset=? AND platform=? AND source=? AND sender=? ORDER BY sent_at DESC,id DESC LIMIT 1",
                            (self.cache.get("dataset", ""), row[0], row[1], row[2]),
                        ).fetchone()[0]
                    )
                    person, basis = resolve_chat(rec, manual, people)
                    key = identity_key(rec)
                    decisions = (
                        [
                            dict(r)
                            for r in c.execute(
                                "SELECT person_id,action,basis FROM source_identity_decisions WHERE identity_key=?",
                                (key,),
                            )
                        ]
                        if "source_identity_decisions" in tables
                        else []
                    )
                    items.append(
                        {
                            "key": key,
                            "platform": row[0],
                            "source": row[1],
                            "account": row[2],
                            "label": rec.get("sender_display_name") or row[2],
                            "person_id": person or "",
                            "basis": basis,
                            "decisions": decisions,
                            "editable": True,
                        }
                    )
        recordings, recording_more = self.recording_identities(offset)
        items.extend(recordings)
        more = more or recording_more
        return {
            "people": list(people.values()),
            "items": items,
            "next_offset": offset + 50 if more else None,
            "version": self.version(),
            "recording_notice": "录音身份沿用原转写人工确认；纠正请使用现有人物标注入口。",
        }

    def decide(self, request, actor):
        required = {"operation_id", "key", "person_id", "action", "basis", "version"}
        if set(request) != required or any(
            not isinstance(request[k], str) for k in required
        ):
            raise ValueError("人物关联请求无效")
        if (
            not 1 <= len(request["operation_id"]) <= 80
            or not 1 <= len(request["basis"].strip()) <= 300
        ):
            raise ValueError("必须填写关联／纠正依据")
        if request["action"] not in {"link", "reject", "unlink"}:
            raise ValueError("人物关联操作无效")
        try:
            platform, source, account = json.loads(request["key"])
        except (ValueError, TypeError):
            raise ValueError("来源身份键无效") from None
        if platform not in {"qq", "wechat"} or any(
            not isinstance(v, str) or not v or len(v) > 200 for v in [source, account]
        ):
            raise ValueError("仅可选择已有稳定聊天账号；录音身份请在原标注处确认")
        if request["key"] != packed([platform, source, account]):
            raise ValueError("来源身份键必须规范化")
        people = self.people()
        if request["person_id"] not in people or not self.chat_path.exists():
            raise ValueError("必须选择已有已确认人物和来源身份")
        with self.cache.connect() as c:
            c.executescript(SCHEMA)
            c.execute("BEGIN IMMEDIATE")
            fingerprint = packed({**request, "actor": actor})
            replay = c.execute(
                "SELECT request,result FROM source_identity_operations WHERE operation_id=?",
                (request["operation_id"],),
            ).fetchone()
            if replay:
                if replay[0] != fingerprint:
                    raise ValueError("操作编号已被不同请求使用")
                return json.loads(replay[1])
            if self.version() != request["version"]:
                raise ValueError("人物映射已变化，请刷新后重试")
            observed = c.execute(
                "SELECT body FROM search_index WHERE dataset=? AND platform=? AND source=? AND sender=? ORDER BY sent_at DESC,id DESC LIMIT 1",
                (self.cache.get("dataset", ""), platform, source, account),
            ).fetchone()
            if not observed:
                raise ValueError("来源账号尚无已授权缓存证据")
            old = c.execute(
                "SELECT person_id FROM user_account_links WHERE platform=? AND source=? AND account=?",
                (platform, source, account),
            ).fetchone()
            person = (
                "self"
                if people[request["person_id"]]["kind"] == "self"
                else request["person_id"]
            )
            current, _ = resolve_chat(json.loads(observed[0]), self.manual(), people)
            if request["action"] == "unlink" and current != request["person_id"]:
                raise ValueError("当前关联与请求不一致")
            if request["action"] != "reject" or current == request["person_id"]:
                value = person if request["action"] == "link" else None
                c.execute(
                    "INSERT INTO user_account_links VALUES(?,?,?,?) ON CONFLICT(platform,source,account) DO UPDATE SET person_id=excluded.person_id",
                    (platform, source, account, value),
                )
                c.execute(
                    "INSERT INTO chat_identity_manual_audit(platform,source,account,previous_person,person_id,observed_at) VALUES(?,?,?,?,?,?)",
                    (
                        platform,
                        source,
                        account,
                        old[0] if old else None,
                        value,
                        time.time(),
                    ),
                )
            generation = self.cache.get("generation", 0) + 1
            self.cache._put(c, "generation", generation)
            c.execute(
                "INSERT INTO source_identity_decisions VALUES(?,?,?,?,?) ON CONFLICT(identity_key,person_id) DO UPDATE SET action=excluded.action,basis=excluded.basis,operation_id=excluded.operation_id",
                (
                    request["key"],
                    request["person_id"],
                    request["action"],
                    request["basis"],
                    request["operation_id"],
                ),
            )
            result = {
                "status": "applied",
                "action": request["action"],
                "operation_id": request["operation_id"],
            }
            c.execute(
                "INSERT INTO source_identity_operations VALUES(?,?,?,?,?)",
                (
                    request["operation_id"],
                    fingerprint,
                    packed(result),
                    actor,
                    time.time(),
                ),
            )
        return result
