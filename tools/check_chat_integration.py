"""Finite real Mac smoke check. Reports counts, never chat bodies or credentials."""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from allday_asr.v3.adapters.chat_data.mac import MacChatData
from allday_asr.v3.application.chat_queries import checked_scope
from allday_asr.v3.ports.chat_data import CONTRACT, ChatDataError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope-file", type=Path)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.getenv("ALLDAY_V3_STATE_DIR", "state/v3")),
    )
    parser.add_argument("--max-pages", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 10:
        parser.error("max-pages must be 1..10")
    client = MacChatData(config_path=args.state_dir / "chat/connection.json")
    result = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED",
        "contract_version": CONTRACT,
    }
    started = time.monotonic()
    try:
        status = client.request("/v1/status")
        contract = client.request("/v1/contract")
        coverage = client.request("/v1/coverage")
        if (
            status.get("api_contract_version") != CONTRACT
            or contract.get("api_contract_version") != CONTRACT
            or status.get("schema_version") != 1
        ):
            raise ChatDataError("CHAT_CONTRACT_UNSUPPORTED")
        if status.get("status") != "ready":
            raise ChatDataError("INITIALIZING")
        result.update(
            status="READY_FOR_LIVE_INTEGRATION",
            service_status=status.get("status"),
            service_version=status.get("service_version"),
            schema_version=status.get("schema_version"),
            sources=[
                {
                    "platform": s.get("platform"),
                    "captured_records": s.get("captured_records"),
                    "error": s.get("error"),
                }
                for s in coverage.get("sources", [])
            ],
            fixed_question_acceptance="NOT_EXECUTED",
        )
        if args.scope_file:
            scope = checked_scope(
                json.loads(args.scope_file.read_text(encoding="utf-8")),
                default_recent=True,
            )
            if not {"platform", "source_account_id", "conversation_id"} <= scope.keys():
                raise ChatDataError("EXPLICIT_FIXED_SCOPE_REQUIRED")
            snapshot = client.request("/v1/snapshots", {"scope": scope})
            cursor = None
            count = 0
            complete = False
            anchor = None
            for _ in range(args.max_pages):
                if time.monotonic() - started > 60:
                    raise ChatDataError("LIVE_CHECK_TIME_BUDGET_REACHED")
                page = client.request(
                    "/v1/messages",
                    scope=scope,
                    snapshot_id=snapshot["snapshot_id"],
                    cursor=cursor,
                    limit=25,
                )
                count += len(page["items"])
                anchor = anchor or next(iter(page["items"]), None)
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    complete = True
                    break
            result["snapshot"] = {
                "records": count,
                "complete": complete,
                "scope_is_user_selected": True,
            }
            changes = client.request(
                "/v1/changes", scope=scope, cursor=snapshot["boundary_cursor"], limit=25
            )
            result["changes"] = {
                "count": len(changes["items"]),
                "has_more": changes["has_more"],
            }
            if anchor:
                context = client.request(
                    "/v1/context",
                    {"record_id": anchor["record_id"], "before": 2, "after": 2},
                )
                assert all(
                    r["platform"] == anchor["platform"]
                    and r["source_account_id"] == anchor["source_account_id"]
                    and r["conversation_id"] == anchor["conversation_id"]
                    for r in context["items"]
                )
                batch = client.request(
                    "/v1/records",
                    {
                        "items": [
                            {
                                "record_id": anchor["record_id"],
                                "record_revision": anchor["record_revision"],
                            },
                            {"record_id": "rec:" + "0" * 64, "record_revision": 1},
                        ]
                    },
                )
                result["context_same_identity"] = True
                result["batch_statuses"] = [item["status"] for item in batch["items"]]
        result["note"] = "Smoke checks alone do not establish CHAT_QUERY_V1_READY."
    except ChatDataError as exc:
        result["error"] = exc.code
    except (OSError, ValueError, KeyError, AssertionError):
        result["error"] = "LIVE_CHECK_FAILED"
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    out = args.state_dir / "chat/live-acceptance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "READY_FOR_LIVE_INTEGRATION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
