"""Explicit one-day run; no ASR, followup extraction, history scan or automatic retry."""

import argparse
import json
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
    parser.add_argument("--timezone", default="Asia/Singapore")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    db = V3Database(args.state / "core.sqlite3")

    def factory():
        return SqliteUnitOfWork(db)

    root = args.state / "unified-review"
    sources = UnifiedReviewSources(db, factory, args.state / "chat/cache.sqlite3")
    model = UnifiedReviewModel(root)
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
    result = service.generate(args.date, args.timezone)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "review.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# {args.date} 聊天＋录音每日回顾",
        f"时区：{args.timezone}",
        f"状态：{result['status']}；生成时间：{result.get('generated_at', '未生成')}",
        "资料覆盖：" + json.dumps(result.get("coverage"), ensure_ascii=False),
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
            lines += [
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
