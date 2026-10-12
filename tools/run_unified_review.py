"""Explicit one-day run; no ASR, followup extraction, history scan or automatic retry."""

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.unified_review_sources import UnifiedReviewSources
from allday_asr.v3.adapters.codex.unified_review_model import UnifiedReviewModel
from allday_asr.v3.application.unified_review import UnifiedReviewService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--cached-result",
        type=Path,
        help="Reuse an existing exact-input provider response; zero model calls",
    )
    parser.add_argument(
        "--revision",
        type=Path,
        help="Explicit local headline/claims/limitations JSON; zero model calls",
    )
    parser.add_argument(
        "--expected-input-sha256", help="Required unchanged input digest for --revision"
    )
    parser.add_argument("--reason", help="Local correction reason for --revision")
    parser.add_argument("--timezone", default="Asia/Singapore")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    db = V3Database(args.state / "core.sqlite3")

    def factory():
        return SqliteUnitOfWork(db)

    root = args.state / "unified-review"
    sources = UnifiedReviewSources(db, factory, args.state / "chat/cache.sqlite3")
    if args.revision and args.cached_result:
        parser.error("--revision and --cached-result are mutually exclusive")
    if args.revision and (not args.expected_input_sha256 or not args.reason):
        parser.error("--revision requires --expected-input-sha256 and --reason")

    def forbidden_model(inputs):
        raise AssertionError("local revision cannot invoke a model")

    model = forbidden_model if args.revision else UnifiedReviewModel(root)
    if args.cached_result:
        result_path = args.cached_result
        request_path = result_path.with_name(
            result_path.name.replace(".result.json", ".request.json")
        )
        request_data = json.loads(request_path.read_text(encoding="utf-8"))
        saved = json.loads(result_path.read_text(encoding="utf-8"))
        if saved.get("status") != "completed":
            raise ValueError("cached provider response is unavailable")

        def cached_model(inputs):
            from allday_asr.v3.domain.chat_data import packed

            if request_data["prompt"] != packed(inputs):
                raise ValueError("cached response belongs to different input")
            return json.loads(saved["result"]["final_response"]), {
                "provider": "codex-cached-response",
                "model": request_data["model"],
                "response_ref": str(result_path),
                "remote_calls_this_run": 0,
            }

        model = cached_model
    service = UnifiedReviewService(sources, factory, model, root)
    if args.revision:
        result = service.revise(
            args.date,
            args.timezone,
            json.loads(args.revision.read_text(encoding="utf-8")),
            expected_input_sha256=args.expected_input_sha256,
            reason=args.reason,
        )
    else:
        result = service.generate(args.date, args.timezone)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "review.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# {args.date} 聊天＋录音每日回顾",
        f"时区：{args.timezone}",
        f"状态：{result['status']}；生成时间：{result.get('generated_at', '未生成')}",
        f"本地修订时间：{result.get('revised_at', '未修订')}",
        "资料覆盖：" + json.dumps(result.get("coverage"), ensure_ascii=False),
        "聊天选取数／缓存数只表示输入筛选范围，不是召回率；未入选记录不等于没有价值。",
        "",
    ]
    evidence = {e["evidence_id"]: e for e in result.get("evidence", [])}
    for i, claim in enumerate(result.get("claims", []), 1):
        lines += [
            f"## {i}. {claim['text']}",
            f"栏目：{claim['section']}；依据性质：{claim['certainty']}；视角：{claim['perspective']}",
        ]
        for ref in claim["evidence"]:
            e = evidence[ref["evidence_id"]]
            stamp = e.get("sent_at", e.get("start_at"))
            occurrence = (
                datetime.fromtimestamp(stamp, ZoneInfo(args.timezone))
                if isinstance(stamp, (int, float))
                else datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(
                    ZoneInfo(args.timezone)
                )
            )
            speaker = (
                e.get("speaker")
                or e.get("participant", {}).get("label")
                or "身份未确认"
            )
            lines += [
                f"- 发送者／说话人：{speaker}；时间：{occurrence.isoformat()}",
                f"- 原话：{ref['quote']}",
                "- 来源索引："
                + json.dumps(
                    {k: v for k, v in e.items() if k not in {"text", "context"}},
                    ensure_ascii=False,
                ),
            ]
        if claim["event_ids"]:
            lines += ["关联事项：" + ", ".join(claim["event_ids"])]
        lines += [""]
    lines += ["资料限制：", *result.get("limitations", [])]
    (args.output / "review.md").write_text("\n".join(lines), encoding="utf-8")
    (args.output / "source-index.json").write_text(
        json.dumps(result.get("evidence", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "date": args.date,
                "coverage": result.get("coverage"),
                "claims": len(result.get("claims", [])),
                "reused": result.get("reused", False),
                "error": result.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
