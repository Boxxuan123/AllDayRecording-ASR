"""Export an existing local review package through the shared semantic projection.

No source retrieval, model execution, database mutation or raw request export.
"""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import zipfile
from allday_asr.v3.domain.chat_projection import semantic_record, semantic_value


def export(source, output, *, decisions=None, checks=None):
    output.mkdir(parents=True, exist_ok=False)

    def load(name):
        return json.loads((source / name).read_text(encoding="utf8"))

    raw_records = load("source_records.json")
    records = []
    for r in raw_records["records"]:
        view = semantic_record(r)
        view.update(
            {
                k: r[k]
                for k in (
                    "sent_at_utc",
                    "source_timezone",
                    "source_label",
                    "roles",
                    "coverage_bucket",
                )
                if k in r
            }
        )
        view["raw_text_sha256"] = sha256((r.get("text") or "").encode()).hexdigest()
        records.append(view)
    candidates = semantic_value(load("candidates.json"))
    for c in candidates["candidates"]:
        c["event_id_context"] = "ISOLATED_REFERENCE_ONLY_NOT_A_PRODUCTION_TARGET"
        if decisions:
            c["personal_admission"] = decisions[c["source_key"]]
            c["review_status"] = "RELEVANCE_REVIEWED_SEMANTIC_CORRECTNESS_NOT_CERTIFIED"
    files = {
        "candidates.json": candidates,
        "source_records.json": {
            **raw_records,
            "records": records,
            "projection": "semantic-access-material-removed-v1",
        },
    }
    for name in ("coverage.json", "source_calibration.json", "input_metrics.json"):
        files[name] = semantic_value(load(name))
    if checks is not None:
        files["offline_checks.json"] = checks
    for name, value in files.items():
        (output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
    lines = [
        "# 聊天跟进相关性修复审核包",
        "",
        "只复用现有本地审核包，不新增检索或模型调用。",
        "此包只含原审核候选和必要的已缓存证据；原始库未改写。",
        "个人准入见每条记录；未知相关性不制造待审核卡，未经复核的原分类不视为正确。",
        "相关性处置不证明候选提取或语义解释全部正确。历史最终状态仍未知。",
        "本人发送者映射及来源时区缺项集中见 source_calibration.json；不按昵称或本机时区推断。",
        "覆盖缺口及已分析截止位置保留在 coverage.json，不推断其后完整性。",
        "生产状态、重放结果和请求检查见 offline_checks.json。原事件 ID 只作隔离审计参考。",
        "",
    ]
    for c in candidates["candidates"]:
        lines += [
            "## " + c["review_id"],
            "",
            c["current_title"],
            "",
            "个人准入："
            + json.dumps(c.get("personal_admission", {}), ensure_ascii=False),
            "承担者：" + str(c.get("actor_account_id")),
            "时间原话：" + str(c.get("original_time_expression")),
            "解析：" + json.dumps(c.get("parsed_time"), ensure_ascii=False),
            "人工覆盖："
            + str(c.get("human_override"))
            + "；历史："
            + str(c.get("historical")),
            "未覆盖后续：" + str(c.get("later_status_limit")),
            "",
        ]
        for e in c["evidence"]:
            lines += [
                "```text",
                str(e.get("record_id")) + " · " + str(e.get("sender_account_id")),
                e.get("quote", ""),
                "```",
                "",
            ]
        lines += [
            "必要缓存上下文 ID："
            + json.dumps(c.get("context_record_ids"), ensure_ascii=False),
            "",
        ]
    lines += [
        "# 必要缓存上下文",
        "",
        "引用开始/结束标记区分当前发言和引用作者；未解析内容不回退原始 XML。",
        "",
    ]
    for r in records:
        lines += [
            "### " + r["record_id"],
            str(r.get("sender_account_id")) + " · " + str(r.get("sent_at_utc")),
            "```text",
            r.get("text") or "[无正文]",
            "```",
            "",
        ]
    (output / "REVIEW.md").write_text("\n".join(lines), encoding="utf8")
    manifest = {
        "raw_database_included": False,
        "raw_requests_included": False,
        "new_model_calls": 0,
        "files": [
            {
                "name": p.name,
                "bytes": p.stat().st_size,
                "sha256": sha256(p.read_bytes()).hexdigest(),
            }
            for p in sorted(output.iterdir())
        ],
    }
    (output / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf8"
    )
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(output.iterdir()):
            z.write(p, p.name)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(export(args.source, args.output))
