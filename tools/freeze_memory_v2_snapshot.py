"""Freeze complete authorized native windows; evaluation labels are never inputs."""

import argparse
import hashlib
import json
from pathlib import Path
from allday_asr.v3.adapters.chat_data.mac import MacChatData


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--connection", type=Path, required=True)
    p.add_argument("--windows", type=Path, required=True)
    p.add_argument("--destination", type=Path, required=True)
    p.add_argument("--reference", type=int, required=True)
    args = p.parse_args()
    if args.destination.exists():
        raise SystemExit("Refusing to overwrite a frozen snapshot")
    windows = json.loads(args.windows.read_text("utf-8"))
    assert 1 <= len(windows) <= 20
    source = MacChatData(config_path=args.connection, timeout=10)
    status = source.request("/v1/status")
    rows = {}
    pages = []
    for scope in windows:
        assert set(scope) <= set(
            ("platform", "source_account_id", "conversation_id", "sent_from", "sent_to")
        )
        assert all(
            k in scope
            for k in (
                "platform",
                "source_account_id",
                "conversation_id",
                "sent_from",
                "sent_to",
            )
        )
        assert 0 <= scope["sent_to"] - scope["sent_from"] < 4 * 86400
        cursor = None
        while True:
            assert len(pages) < 80
            response = source.request(
                "/v1/messages", scope=scope, limit=100, cursor=cursor
            )
            assert (
                response.get("dataset_id", status["dataset_id"]) == status["dataset_id"]
            )
            pages.append({"scope": scope, "response": response})
            for r in response.get("items", []):
                r = r.get("record", r)
                rows[
                    (
                        r["platform"],
                        r["source_account_id"],
                        r["conversation_id"],
                        r["record_id"],
                    )
                ] = r
            assert (
                len(rows) <= 2000
                and sum(len(r.get("text") or "") for r in rows.values()) <= 500000
            )
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
            assert cursor
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "dataset_id": status["dataset_id"],
        "reference": args.reference,
        "windows": windows,
        "source_status": status,
        "records": sorted(rows.values(), key=lambda r: (r["sent_at"], r["record_id"])),
    }
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
    args.destination.write_bytes(raw)
    args.destination.with_suffix(".pages.json").write_text(
        json.dumps(pages, ensure_ascii=False), "utf-8"
    )
    manifest = {
        "records": len(rows),
        "windows": len(windows),
        "source_requests": len(pages) + 1,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "selection": "full native conversation windows",
    }
    args.destination.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2), "utf-8"
    )
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
