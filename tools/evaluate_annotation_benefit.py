"""Score local, independently labelled before/after predictions. No audio upload.

Usage: python tools/evaluate_annotation_benefit.py manifest.json results.jsonl
See docs/evaluation-guide.md for the input contract and limitations.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def evaluate(manifest: dict, rows: list[dict]) -> dict:
    if not manifest.get("model") or not manifest.get("model_version") or not manifest.get("conditions"):
        raise ValueError("固定模型、版本与处理条件不能为空")
    enrollment = manifest.get("enrollment", [])
    if not enrollment or not rows:
        raise ValueError("必须提供建库集和独立测试集；空集合不产生效果结论")
    required = {"evidence_id", "conversation_id", "date"}
    if any(not required <= set(r) or any(not r[k] for k in required) for r in enrollment + rows):
        raise ValueError("每条证据必须标明证据 ID、真实对话 ID 和日期")
    for field in required:
        if {r[field] for r in enrollment} & {r[field] for r in rows}:
            raise ValueError(f"建库与测试泄漏：{field}")
    if len({r['evidence_id'] for r in rows}) != len(rows):
        raise ValueError("测试证据重复")
    known = set(manifest.get("known_person_ids", []))
    if not known or manifest.get("self_person_id") not in known:
        raise ValueError("需固定本人及熟人的真实人物 ID")
    if any(not r.get("truth_person_id") or "before" not in r or "after" not in r for r in rows):
        raise ValueError("测试条目缺少人工真值或前后预测；未知预测应显式填 null")
    def metrics(field):
        familiar = [r for r in rows if r['truth_person_id'] in known]
        strangers = [r for r in rows if r['truth_person_id'] not in known]
        def ratio(n, d):
            return n / d if d else None
        return {
            "known_identity_accuracy": ratio(sum(r[field] == r['truth_person_id'] for r in familiar), len(familiar)),
            "known_left_unknown_rate": ratio(sum(r[field] is None for r in familiar), len(familiar)),
            "stranger_false_known_rate": ratio(sum(r[field] in known for r in strangers), len(strangers)),
            "per_person": {p: {"count": sum(r['truth_person_id'] == p for r in rows),
                "correct": sum(r['truth_person_id'] == p and r[field] == p for r in rows)} for p in sorted(known)},
            "downstream": {kind: {
                "evaluated": sum(type(r.get(f'{field}_{kind}_correct')) is bool for r in rows),
                "correct": sum(r.get(f'{field}_{kind}_correct') is True for r in rows)}
                for kind in ('reminder', 'event', 'memory')},
            "review_requests": sum(r.get(f'{field}_review_requests', 0) for r in rows),
        }
    before, after = metrics('before'), metrics('after')
    minutes = manifest.get('review_minutes')
    improved = sum(r['before'] != r['truth_person_id'] and r['after'] == r['truth_person_id'] for r in rows)
    regressed = sum(r['before'] == r['truth_person_id'] and r['after'] != r['truth_person_id'] for r in rows)
    return {"model": manifest['model'], "model_version": manifest['model_version'],
        "conditions": manifest['conditions'], "test_count": len(rows),
        "before": before, "after": after, "improved": improved, "regressed": regressed,
        "net_correct_per_review_minute": (improved-regressed)/minutes if isinstance(minutes, (int,float)) and minutes > 0 else None,
        "limitations": ["此工具校验划分并统计实际运行导出的预测；不生成预测，不证明标注真值正确。",
                        "未提供的下游评估显示 evaluated=0；分母为零显示 null，不能视为零错误。"]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('predictions', type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.manifest.read_text(encoding='utf-8')),
                      [json.loads(line) for line in args.predictions.read_text(encoding='utf-8').splitlines() if line.strip()])
    print(json.dumps(result, ensure_ascii=False, indent=2))
