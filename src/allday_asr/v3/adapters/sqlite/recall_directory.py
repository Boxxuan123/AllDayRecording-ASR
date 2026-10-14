"""Source-native conversation catalogue; derived metadata, never person decisions."""

import hashlib
import json
import time
from pathlib import Path
from allday_asr.v3.ports.chat_data import ChatDataError

FIELDS = ("remark", "remark_name", "nickname", "display_name", "group_card")


def entry(row, dataset):
    if not isinstance(row, dict):
        return None
    platform = row.get("platform")
    account = row.get("source_account_id")
    conversation = row.get("conversation_id", row.get("id"))
    if platform not in {"qq", "wechat"} or not all(
        isinstance(v, str) and 0 < len(v) <= 200 for v in [account, conversation]
    ):
        return None
    if row.get("conversation_type") not in {"c2c", "group"}:
        return None
    names = []
    for field in FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value.strip() and value not in names:
            names.append(value.strip()[:100])
    aliases = row.get("aliases", [])
    if isinstance(aliases, list):
        names.extend(a[:100] for a in aliases[:10] if isinstance(a, str) and a.strip())
    if not names:
        return None
    stable = [dataset, platform, account, conversation]
    key = hashlib.sha256(json.dumps(stable, ensure_ascii=False).encode()).hexdigest()[
        :32
    ]
    sender = row.get("peer_account_id", row.get("sender_account_id"))
    return {
        "key": key,
        "platform": platform,
        "source_account_id": account,
        "conversation_id": conversation,
        "conversation_type": row["conversation_type"],
        "sender_account_id": sender
        if isinstance(sender, str) and 0 < len(sender) <= 200
        else None,
        "display_name": names[0],
        "names": list(dict.fromkeys(names)),
        "basis": "native conversation directory",
        "dataset": dataset,
    }


class RecallDirectory:
    def __init__(self, identities, source, root):
        self.ids, self.source = identities, source
        self.path = Path(root) / "chat/recall-directory.json"

    def cached(self):
        rows = []
        dataset = (
            self.ids.cache.get("dataset", "") if self.ids.chat_path.exists() else ""
        )
        if self.path.exists() and self.path.stat().st_size < 2000000:
            try:
                value = json.loads(self.path.read_text("utf-8"))
                if isinstance(value, dict) and (
                    not dataset or value.get("dataset") == dataset
                ):
                    rows.extend(value.get("items", []))
            except (ValueError, OSError):
                pass
        if self.ids.chat_path.exists():
            with self.ids.cache.connect() as c:
                c.execute("PRAGMA query_only=ON")
                # Reuse existing paged directory metadata, not message text/nicknames.
                for item in c.execute(
                    "SELECT value FROM metadata WHERE key LIKE 'directory:conversations:%' LIMIT 30"
                ):
                    page = json.loads(item[0])
                    if page.get("_cache_dataset") == dataset:
                        rows.extend(
                            v for r in page.get("items", []) if (v := entry(r, dataset))
                        )
                # Stable historical private-conversation labels are source metadata.
                scopes = c.execute(
                    "SELECT DISTINCT platform,source,conversation FROM search_index WHERE dataset=? LIMIT 501",
                    (dataset,),
                ).fetchall()
                for platform, account, conversation in scopes:
                    r = c.execute(
                        "SELECT json_extract(body,'$.conversation_type'),json_extract(body,'$.conversation_display_name') FROM search_index WHERE dataset=? AND platform=? AND source=? AND conversation=? LIMIT 1",
                        (dataset, platform, account, conversation),
                    ).fetchone()
                    value = entry(
                        {
                            "platform": platform,
                            "source_account_id": account,
                            "id": conversation,
                            "conversation_type": r[0],
                            "display_name": r[1],
                        },
                        dataset,
                    )
                    if value:
                        rows.append(value)
        return list(
            {r["key"]: r for r in rows if isinstance(r, dict) and r.get("key")}.values()
        )[:5000]

    def load(self, platform, live, cancel, deadline):
        rows = self.cached()
        notes, requests = [], 0
        state = {}
        if self.path.exists() and self.path.stat().st_size < 2000000:
            try:
                state = json.loads(self.path.read_text("utf-8"))
            except (ValueError, OSError):
                pass
        pages = state.get("pages", {})
        sources = [platform] if platform in {"qq", "wechat"} else ["wechat", "qq"]
        if not live:
            for r in rows:
                r["directory_complete"] = bool(
                    pages.get(r["platform"], {}).get("complete")
                )
            if any(not pages.get(p, {}).get("complete") for p in sources):
                notes.append("本次只使用已缓存的联系人目录，覆盖可能不足。")
            return rows, notes, requests

        def call(route, **kwargs):
            nonlocal requests
            if cancel.is_set() or time.monotonic() > deadline:
                raise ChatDataError("QUERY_TIMEOUT", 408)
            if requests >= 6:
                raise ChatDataError("DIRECTORY_BUDGET_REACHED", 429)
            requests += 1
            return self.source.request(route, **kwargs)

        dataset = state.get("dataset", "")
        try:
            status = call("/v1/status")
            dataset = status["dataset_id"]
            expected = (
                self.ids.cache.get("dataset", "") if self.ids.chat_path.exists() else ""
            )
            if expected and expected != dataset:
                raise ChatDataError("DATASET_MISMATCH", 409)
            self.ids.directory_dataset = dataset
            rows = [r for r in rows if r["dataset"] == dataset]
            if state.get("dataset") != dataset:
                pages = {}
            # Resume immutable directory cursors within their one-hour lifetime.
            # Complete pages are reused briefly; refreshes never infer uniqueness
            # from a partial directory. Each query still makes at most six calls.
            for source in sources:
                saved = pages.get(source, {})
                if time.time() - saved.get("updated_at", 0) > (
                    300 if saved.get("complete") else 3000
                ):
                    pages[source] = {}
            while requests < 6:
                pending = [p for p in sources if not pages.get(p, {}).get("complete")]
                if not pending:
                    break
                for source in pending:
                    if requests >= 6:
                        break
                    saved = pages.get(source, {})
                    cursor = saved.get("cursor")
                    page = call(
                        "/v1/conversations",
                        scope={"platform": source},
                        limit=100,
                        cursor=cursor,
                    )
                    items = page.get("items")
                    if not isinstance(items, list) or len(items) > 100:
                        raise ChatDataError("INVALID_DIRECTORY_PAGE", 502)
                    for item in items:
                        if not isinstance(item, dict) or item.get("platform") != source:
                            raise ChatDataError("DIRECTORY_NAMESPACE_MISMATCH", 502)
                        value = entry(item, dataset)
                        if value:
                            rows.append(value)
                    rows = list({r["key"]: r for r in rows}.values())
                    complete = not page.get("has_more")
                    next_cursor = page.get("next_cursor")
                    if not complete and (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or next_cursor == cursor
                    ):
                        raise ChatDataError("INVALID_DIRECTORY_CURSOR", 502)
                    pages[source] = {
                        "complete": complete,
                        "cursor": next_cursor,
                        "updated_at": time.time(),
                    }
                    if len(rows) > 5000:
                        pages[source]["complete"] = False
                        rows = rows[:5000]
                        raise ChatDataError("DIRECTORY_SIZE_LIMIT", 429)
        except (ChatDataError, ValueError, KeyError, OSError) as exc:
            notes.append(
                "联系人目录未完整取回；来源可能离线或尚未采集，不能判断联系人不存在。"
            )
            if getattr(exc, "code", "") in {"CURSOR_EXPIRED", "INVALID_CURSOR"}:
                pages = {}
        for r in rows:
            r["directory_complete"] = bool(pages.get(r["platform"], {}).get("complete"))
        if any(not pages.get(p, {}).get("complete") for p in sources):
            notes.append(
                "联系人目录仅取回部分页；匹配对象需确认，未匹配不表示联系人不存在。"
            )
        if dataset and rows:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            part = self.path.with_suffix(".json.part")
            part.write_text(
                json.dumps(
                    {
                        "dataset": dataset,
                        "updated_at": time.time(),
                        "pages": pages,
                        "items": rows,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            part.replace(self.path)
        return rows, notes, requests

    def linked_many(self, contacts, person):
        from allday_asr.v3.domain.chat_data import packed
        from threading import Event
        from .recall_conversations import conversation_scopes

        manual = self.ids.manual()
        scopes = conversation_scopes(
            self.ids,
            {"person_id": person, "source": "all"},
            Event(),
            time.monotonic() + 2,
        )
        keys = {
            (s["platform"], s["source_account_id"], s["conversation_id"])
            for s in scopes
        }
        result = []
        for contact in contacts:
            sender = contact.get("sender_account_id")
            linked = (
                manual.get(
                    packed([contact["platform"], contact["source_account_id"], sender])
                )
                == person
                if sender
                else contact["conversation_type"] == "c2c"
                and (
                    contact["platform"],
                    contact["source_account_id"],
                    contact["conversation_id"],
                )
                in keys
            )
            if linked:
                result.append(contact)
        return result
