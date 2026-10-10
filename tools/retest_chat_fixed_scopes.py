"""Bounded full-page retest against user-selected private Mac scopes; no model calls."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from allday_asr.v3.adapters.chat_data.mac import MacChatData
from allday_asr.v3.application.chat_queries import checked_scope
from allday_asr.v3.ports.chat_data import CONTRACT, ChatDataError


def require(condition, code):
    if not condition:
        raise ChatDataError(code, 502)


def matches(record, scope):
    for key, value in scope.items():
        if key == "sent_from":
            if record["sent_at"] is None or record["sent_at"] < value:
                return False
        elif key == "sent_to":
            if record["sent_at"] is None or record["sent_at"] > value:
                return False
        elif key == "keyword":
            if value not in (record["text"] or ""):
                return False
        elif record[key] != value:
            return False
    return True


class FixedScopeRetest:
    def __init__(self, source, *, max_pages=100, seconds=300):
        self.source = source
        self.max_pages = max_pages
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.timings = []
        self.requests = 0
        self.report = {"status": "BLOCKED", "scopes": [], "model_calls": 0}
        self.private = []

    def call(self, route, body=None, **query):
        if time.monotonic() >= self.deadline:
            raise ChatDataError("RETEST_TIME_BUDGET")
        self.requests += 1
        started = time.monotonic()
        try:
            return self.source.request(route, body, **query)
        finally:
            self.timings.append(time.monotonic() - started)

    def pages(self, scope, snapshot=None):
        records, seen, timings = [], set(), []
        cursor = None
        for number in range(self.max_pages):
            started = time.monotonic()
            page = self.call(
                "/v1/messages",
                scope=scope,
                snapshot_id=snapshot,
                cursor=cursor,
                limit=2,
            )
            timings.append(time.monotonic() - started)
            require(
                isinstance(page.get("items"), list)
                and len(page["items"]) <= 2
                and type(page.get("has_more")) is bool,
                "INVALID_CHAT_PAGE",
            )
            for record in page["items"]:
                require(record["record_id"] not in seen, "RETEST_DUPLICATE_RECORD")
                require(matches(record, scope), "RETEST_RECORD_OUTSIDE_SCOPE")
                require(record["record_revision"] == 1, "RETEST_REVISION_CHANGED")
                seen.add(record["record_id"])
                records.append(record)
            next_cursor = page.get("next_cursor")
            if not page["has_more"]:
                if next_cursor:
                    for _ in range(2):
                        terminal = self.call(
                            "/v1/messages",
                            scope=scope,
                            snapshot_id=snapshot,
                            cursor=next_cursor,
                            limit=2,
                        )
                        require(
                            terminal["items"] == [] and terminal["has_more"] is False,
                            "RETEST_TERMINAL_NOT_EMPTY",
                        )
                return records, {
                    "records": len(records),
                    "pages": number + 1,
                    "complete": True,
                    "slowest_page_seconds": round(max(timings), 6),
                    "terminal_empty_checked": bool(next_cursor),
                }
            require(next_cursor and next_cursor != cursor, "NON_ADVANCING_CURSOR")
            cursor = next_cursor
        raise ChatDataError("RETEST_PAGE_BUDGET")

    def scope(self, raw, ordinal):
        scope = checked_scope(raw)
        require(
            {"platform", "source_account_id", "conversation_id", "sent_from", "sent_to"}
            <= scope.keys()
            and "keyword" not in scope,
            "EXPLICIT_FIXED_SCOPE_REQUIRED",
        )
        snapshot = self.call("/v1/snapshots", {"scope": scope})
        ordinary, ordinary_stats = self.pages(scope)
        frozen, frozen_stats = self.pages(scope, snapshot["snapshot_id"])
        # Inserts observed between boundaries are not silently called identical.
        require(ordinary == frozen, "RETEST_BOUNDARIES_DIFFER_OR_BODY_CHANGED")
        for offset in range(0, len(frozen), 100):
            chunk = frozen[offset : offset + 100]
            batch = self.call(
                "/v1/records",
                {
                    "items": [
                        {
                            "record_id": r["record_id"],
                            "record_revision": r["record_revision"],
                        }
                        for r in chunk
                    ]
                },
            )
            require(len(batch["items"]) == len(chunk), "RETEST_BATCH_MISSING")
            for item, record in zip(batch["items"], chunk, strict=True):
                require(
                    item["status"] == "found" and item["record"] == record,
                    "RETEST_BATCH_BODY_MISMATCH",
                )
        if frozen:
            anchor = frozen[0]
            context = self.call(
                "/v1/context",
                {
                    "record_id": anchor["record_id"],
                    "before": 2,
                    "after": 2,
                },
            )
            require(anchor in context["items"], "RETEST_CONTEXT_ANCHOR_MISMATCH")
            require(
                all(
                    all(
                        r[k] == anchor[k]
                        for k in (
                            "platform",
                            "source_account_id",
                            "conversation_id",
                        )
                    )
                    for r in context["items"]
                ),
                "CONTEXT_IDENTITY_MISMATCH",
            )
        changes, cursor = [], snapshot["boundary_cursor"]
        for _ in range(10):
            page = self.call("/v1/changes", scope=scope, cursor=cursor, limit=100)
            for change in page["items"]:
                require(
                    change["operation"] == "insert"
                    and matches(change["record"], scope),
                    "RETEST_CHANGE_MISMATCH",
                )
                changes.append(change)
            if not page["has_more"]:
                break
            require(page["next_cursor"] != cursor, "NON_ADVANCING_CURSOR")
            cursor = page["next_cursor"]
        else:
            raise ChatDataError("RETEST_CHANGE_PAGE_BUDGET")
        keywords = ["杭电", "报名", "API", "12345", "report_v2.PDF", "%", "_", "'"]
        text = next((r["text"] for r in frozen if r.get("text")), "")
        if text:
            literal = next(
                (
                    text[i : i + 2]
                    for i in range(len(text) - 1)
                    if all("\u4e00" <= c <= "\u9fff" for c in text[i : i + 2])
                ),
                text[:2],
            )
            keywords.append(literal)
        combinations = []
        for keyword in dict.fromkeys(keywords):
            filtered = scope | {"keyword": keyword}
            if frozen and frozen[0]["sender_account_id"]:
                filtered["sender_account_id"] = frozen[0]["sender_account_id"]
            # A snapshot is bound to its exact scope. A new filter uses its own query cursor.
            found, stats = self.pages(filtered)
            require(
                found == [r for r in frozen if matches(r, filtered)],
                "RETEST_LITERAL_COMBINATION_MISMATCH",
            )
            combinations.append(stats)
        self.report["scopes"].append(
            {
                "ordinal": ordinal,
                "platform": scope["platform"],
                "ordinary": ordinary_stats,
                "snapshot": frozen_stats,
                "body_and_id_sets_equal": True,
                "batch_read_exact": True,
                "context_same_identity": bool(frozen),
                "changes": len(changes),
                "combination_checks": len(combinations),
                "combination_nonempty": sum(r["records"] > 0 for r in combinations),
            }
        )
        self.private.append(
            {
                "scope": scope,
                "snapshot": snapshot,
                "records": frozen,
                "changes": changes,
            }
        )

    def run(self, scopes, expected_service="1.0.1"):
        try:
            require(
                isinstance(scopes, list) and 1 <= len(scopes) <= 5,
                "SELECT_ONE_TO_FIVE_CONVERSATIONS",
            )
            status = self.call("/v1/status")
            contract = self.call("/v1/contract")
            require(status.get("status") == "ready", "INITIALIZING")
            require(
                status.get("api_contract_version") == CONTRACT
                and contract.get("api_contract_version") == CONTRACT
                and status.get("schema_version") == 1,
                "CHAT_CONTRACT_UNSUPPORTED",
            )
            require(
                status.get("service_version") == expected_service,
                "RETEST_SERVICE_VERSION_MISMATCH",
            )
            self.report.update(
                service_version=status["service_version"],
                schema_version=1,
                contract_version=CONTRACT,
            )
            for ordinal, scope in enumerate(scopes, 1):
                self.scope(scope, ordinal)
            self.report["status"] = "FIXED_SCOPES_RETEST_PASS"
        except ChatDataError as error:
            self.report["error"] = error.code
        except (ValueError, TypeError, KeyError, OSError):
            self.report["error"] = "RETEST_INVALID_MATERIAL_OR_RESPONSE"
        self.report.update(
            requests=self.requests,
            elapsed_seconds=round(time.monotonic() - self.started, 3),
            slowest_request_seconds=round(max(self.timings, default=0), 6),
        )
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope-file", required=True, type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path("state/v3"))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--service-version", default="1.0.1")
    parser.add_argument("--max-pages", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 100:
        parser.error("max-pages must be 1..100")
    # The tool emits real scopes/bodies only inside the ignored project state tree.
    state_root = (Path(__file__).resolve().parents[1] / "state").resolve()
    if not args.output_dir.resolve().is_relative_to(state_root):
        parser.error("output-dir must be inside this project's private state directory")
    runner = FixedScopeRetest(
        MacChatData(config_path=args.state_dir / "chat/connection.json"),
        max_pages=args.max_pages,
    )
    try:
        scopes = json.loads(args.scope_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        scopes = None
    report = runner.run(scopes, args.service_version)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("summary.json", report),
        ("evidence-private.json", runner.private),
    ):
        (args.output_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["status"] == "FIXED_SCOPES_RETEST_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
