"""One-shot blind evaluation. No answer key is loaded; no production source exists here."""

import argparse
import copy
import hashlib
import json
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.chat_fixture import FixtureMac, record
from allday_asr.v3.adapters.sqlite.database import V3Database
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.bootstrap.history_recall import compose_history_recall
from allday_asr.v3.adapters.codex.recall_investigator import RecallInvestigatorAdapter
from allday_asr.v3.adapters.codex.owned_attempt import OwnedAttempt
from allday_asr.v3.ports.chat_data import ChatDataError

ROUTES = frozenset(
    {
        "/v1/status",
        "/v1/contract",
        "/v1/coverage",
        "/v1/messages",
        "/v1/context",
        "/v1/accounts",
        "/v1/conversations",
        "/v1/records",
    }
)


def save(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def under(root, path):
    root, path = root.resolve(), path.resolve()
    if not path.is_relative_to(root):
        raise ValueError("outside isolated evaluation root")
    return path


def readonly(root, path):
    path = under(root, path)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def seed(case, root):
    root.mkdir(parents=True, exist_ok=False)
    contacts = {c["id"]: c for c in case["contacts"]}
    rows = []
    for msg in case["messages"]:
        c = contacts[msg["conversation"]]
        row = record(
            msg["id"],
            msg["text"],
            conversation=c["id"],
            sender=msg["sender"],
            sent=int(datetime.fromisoformat(msg["at"]).timestamp()),
        )
        row.update(
            conversation_type=c["kind"],
            conversation_display_name=c["name"],
            is_from_self=msg["sender"] == "self-account",
            self_identity_basis="synthetic verified direction",
        )
        rows.append(row)
    with sqlite3.connect(root / "corpus.sqlite3") as conn:
        conn.execute("CREATE TABLE records(body TEXT NOT NULL)")
        conn.execute("CREATE TABLE contacts(body TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO records VALUES (?)",
            [(json.dumps(r, ensure_ascii=False),) for r in rows],
        )
        conn.executemany(
            "INSERT INTO contacts VALUES (?)",
            [
                (
                    json.dumps(
                        {
                            "platform": "qq",
                            "source_account_id": "source-qq",
                            "id": c["id"],
                            "display_name": c["name"],
                            "conversation_type": c["kind"],
                            "peer_account_id": c.get("peer"),
                        },
                        ensure_ascii=False,
                    ),
                )
                for c in case["contacts"]
            ],
        )
    save(
        root / "question.json",
        {k: case[k] for k in ("id", "question", "timezone", "local_now")},
    )
    db = V3Database.open(root / "core.sqlite3")
    cache = ChatCache(root / "chat/cache.sqlite3")
    cache.activate("blind-0811-" + case["id"])
    cache.receive("blind-0811-" + case["id"], rows)
    cache.process(100)
    with db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM persons").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM person_profile_revisions").fetchone()[0]
            == 0
        )


class IsolatedSource:
    def __init__(self, root):
        self.root = root.resolve()
        with readonly(root, root / "corpus.sqlite3") as conn:
            rows = [json.loads(r[0]) for r in conn.execute("SELECT body FROM records")]
            self.contacts = [
                json.loads(r[0]) for r in conn.execute("SELECT body FROM contacts")
            ]
        self.fixture = FixtureMac(rows)
        self.fixture.dataset = (
            "blind-0811-"
            + json.loads((root / "question.json").read_text("utf-8"))["id"]
        )
        self.calls = []
        self.ids = {r["record_id"] for r in rows}

    def request(self, route, body=None, **kwargs):
        if route not in ROUTES:
            raise ChatDataError("READ_CAPABILITY_DENIED", 403)
        for item in [body] if route == "/v1/context" else (body or {}).get("items", []):
            if item.get("record_id") not in self.ids:
                raise ChatDataError("RECORD_NOT_FOUND", 404)
        call = {
            "route": route,
            "body": copy.deepcopy(body),
            "query": copy.deepcopy(kwargs),
        }
        self.calls.append(call)
        save(self.root / "source-steps.json", self.calls)
        if route == "/v1/conversations":
            scope = kwargs.get("scope", {})
            result = {
                "items": [
                    copy.deepcopy(c)
                    for c in self.contacts
                    if all(c.get(k) == v for k, v in scope.items())
                ],
                "has_more": False,
                "next_cursor": None,
            }
        else:
            result = self.fixture.request(route, body, **kwargs)
        call["response"] = copy.deepcopy(result)
        save(self.root / "source-steps.json", self.calls)
        return result


def forbidden(*args, **kwargs):
    raise RuntimeError("production or non-investigation model capability prohibited")


def run_case(root):
    with (root / "FIRST_EXECUTION.lock").open("x") as lock:
        lock.write("No retries. First result is final.\n")
    case = json.loads((root / "question.json").read_text("utf-8"))
    source = IsolatedSource(root)
    adapter = RecallInvestigatorAdapter(root)
    attempts = []
    started = time.monotonic()
    deadline = started + 59  # outer owned job is also bounded at 60 seconds

    def bounded(payload, cancel):
        remaining = math.floor(deadline - time.monotonic())
        if len(attempts) >= 2 or remaining < 1:
            raise TimeoutError("sealed case model budget exhausted")
        payload = {
            **payload,
            "model_budget_seconds": min(25, payload["model_budget_seconds"], remaining),
        }
        attempt = {"input": payload, "status": "reserved", "ordinal": len(attempts) + 1}
        attempts.append(attempt)
        save(root / "model-attempts.json", attempts)
        begin = time.monotonic()
        try:
            value = adapter(payload, cancel)
            attempt.update(status="completed", output=value)
            return value
        except Exception as exc:
            attempt.update(
                status="failed", error_type=type(exc).__name__, error=str(exc)[:1500]
            )
            raise
        finally:
            attempt["elapsed_seconds"] = round(time.monotonic() - begin, 4)
            save(root / "model-attempts.json", attempts)

    service = compose_history_recall(
        V3Database(root / "core.sqlite3"),
        root,
        source=source,
        model=forbidden,
        planner=forbidden,
        investigator=bounded,
    )
    now = int(datetime.fromisoformat(case["local_now"]).timestamp())
    outcome = {"id": case["id"]}
    try:
        with (
            patch(
                "allday_asr.v3.application.history_recall.time",
                SimpleNamespace(time=lambda: now, monotonic=time.monotonic),
            ),
            patch(
                "allday_asr.v3.adapters.chat_data.mac.MacChatData.__init__", forbidden
            ),
        ):
            outcome["response"] = service.execute(
                "blind-evaluation",
                {
                    "action": "query",
                    "query_id": "blind-0811-" + case["id"],
                    "question": case["question"],
                    "source": "qq",
                    "timezone": case["timezone"],
                    "expand": True,
                    "summarize": True,
                },
            )
        outcome["status"] = "RETURNED"
    except Exception as exc:
        outcome.update(
            status="FAILED", error_type=type(exc).__name__, error=str(exc)[:1500]
        )
    outcome.update(
        elapsed_seconds=round(time.monotonic() - started, 4),
        model_attempts=len(attempts),
    )
    save(root / "first-result.json", outcome)


def prepare(suite, output):
    raw = suite.read_bytes()
    data = json.loads(raw)
    assert data["fictional"] is True and data["pre_registered_aliases"] == []
    assert [c["id"] for c in data["cases"]] == ["A", "B", "C"]
    for case in data["cases"]:
        seed(case, output / case["id"])
    save(
        output / "prepared.json",
        {
            "suite_sha256": hashlib.sha256(raw).hexdigest(),
            "case_ids": ["A", "B", "C"],
            "real_model_calls": 0,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "real", "case"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    if args.mode == "prepare":
        prepare(args.suite, root)
        return
    if args.mode == "case":
        run_case(root)
        return
    freeze = json.loads((root / "freeze.json").read_text("utf-8"))
    assert freeze["preflight"] == "PASS"
    for path, expected in freeze["file_hashes"].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected, (
            "freeze changed: " + path
        )
    with (root / "FIRST_EXECUTION.lock").open("x") as lock:
        lock.write("Entire suite may be executed exactly once.\n")
    started = time.monotonic()
    results = []
    for name in ["A", "B", "C"]:
        remaining = 180 - (time.monotonic() - started)
        if remaining < 1:
            results.append({"id": name, "status": "NOT_RUN_BUDGET"})
            continue
        begin = time.monotonic()
        child = OwnedAttempt(
            [
                sys.executable,
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "case",
                "--output",
                str(root / name),
            ],
            root / name / "worker.private.log",
            Path.cwd(),
        )
        code = child.wait(min(60, remaining) - (time.monotonic() - begin))
        save(root / name / "owned-process.json", child.identity)
        value = (
            json.loads((root / name / "first-result.json").read_text("utf-8"))
            if (root / name / "first-result.json").exists()
            else {"id": name, "status": "TIMEOUT" if code == 124 else "WORKER_FAILED"}
        )
        value.update(
            exit_code=code, outer_elapsed_seconds=round(time.monotonic() - begin, 4)
        )
        results.append(value)
        save(
            root / "results.json",
            {
                "results": results,
                "elapsed_seconds": round(time.monotonic() - started, 4),
            },
        )
        print(
            json.dumps(
                {
                    "id": name,
                    "status": value["status"],
                    "elapsed_seconds": value["outer_elapsed_seconds"],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
