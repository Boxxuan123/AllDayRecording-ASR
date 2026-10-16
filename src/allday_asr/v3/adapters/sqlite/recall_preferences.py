"""Explicit owner-scoped query preferences; never source/person identity decisions."""

import hashlib
import json
import unicodedata
from pathlib import Path
from allday_asr.v3.application.file_lock import day_worker_lock


def normalized(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


class RecallPreferences:
    def __init__(self, root):
        self.path = Path(root) / "chat/recall-preferences.json"

    @staticmethod
    def key(owner, mention):
        return hashlib.sha256(
            json.dumps([owner, normalized(mention)], ensure_ascii=False).encode()
        ).hexdigest()

    def read(self):
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > 1_000_000:
            raise ValueError("查询偏好文件过大")
        value = json.loads(self.path.read_text("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("查询偏好格式无效")
        return value

    def change(self, key, value):
        with day_worker_lock(self.path) as acquired:
            if not acquired:
                raise ValueError("查询偏好正在更新，请稍后重试")
            rows = self.read()
            if value is None:
                rows.pop(key, None)
            else:
                if key not in rows and len(rows) >= 1000:
                    raise ValueError("查询偏好已达上限，请先撤销旧偏好")
                rows[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            part = self.path.with_suffix(".json.part")
            part.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            part.replace(self.path)

    def remember(self, owner, mention, contact):
        if (
            not owner
            or not isinstance(mention, str)
            or not 1 <= len(mention.strip()) <= 100
        ):
            raise ValueError("当前问题没有可记忆的明确称呼")
        value = {
            "mention": mention,
            "contact_key": contact["key"],
            "dataset": contact["dataset"],
            "display_name": contact["display_name"],
            "platform": contact["platform"],
        }
        self.change(self.key(owner, mention), value)
        return value

    def resolve(self, owner, mention, source, directory):
        value = self.read().get(self.key(owner, mention))
        if not value:
            return None
        contacts = [
            c
            for c in directory
            if c["key"] == value["contact_key"]
            and c["dataset"] == value["dataset"]
            and source in {"all", c["platform"]}
        ]
        return (contacts[0], value) if len(contacts) == 1 else None

    def forget(self, owner, mention):
        self.change(self.key(owner, mention), None)
