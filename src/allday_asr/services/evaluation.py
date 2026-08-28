from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from allday_asr.paths import EVALUATION_DIR, recording_output_dir
from allday_asr.storage.database import Database


EVALUATION_FORMAT = "AllDayRecording evaluation truth v1"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class EvaluationTemplateSummary:
    recording_id: int
    item_count: int
    speech_seconds: float
    truth_path: Path
    instructions_path: Path


@dataclass(frozen=True)
class EvaluationSummary:
    evaluation_run_id: int
    recording_id: int
    item_count: int
    metrics: dict
    report_json_path: Path
    report_markdown_path: Path


def create_evaluation_template(
    database: Database,
    recording_id: int,
    *,
    name: str,
    start_ms: int = 0,
    end_ms: int | None = None,
    force: bool = False,
) -> EvaluationTemplateSummary:
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("name 只能包含字母、数字、点、下划线和连字符，最长 64 字符")
    if start_ms < 0 or (end_ms is not None and end_ms <= start_ms):
        raise ValueError("评测时间范围无效")
    recording = database.get_recording(recording_id)
    effective_end = end_ms if end_ms is not None else int(recording["duration_ms"])
    segments = [
        segment
        for segment in database.all_segments(recording_id, completed_only=True)
        if int(segment["start_ms"]) < effective_end
        and int(segment["end_ms"]) > start_ms
    ]
    if not segments:
        raise RuntimeError("指定范围内没有已完成转写的语音片段")

    root = EVALUATION_DIR / f"recording-{recording_id:06d}"
    root.mkdir(parents=True, exist_ok=True)
    truth_path = root / f"{name}.jsonl"
    instructions_path = root / f"{name}-README.md"
    if truth_path.exists() and not force:
        raise FileExistsError(f"评测真值已存在，不会覆盖：{truth_path}")

    metadata = {
        "type": "metadata",
        "format": EVALUATION_FORMAT,
        "name": name,
        "recording_id": recording_id,
        "recorded_at": recording["recorded_at"],
        "timezone": recording["timezone"],
        "start_ms": start_ms,
        "end_ms": effective_end,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rows = [
        {
            "type": "segment",
            "include": True,
            "segment_id": int(segment["id"]),
            "start_ms": int(segment["start_ms"]),
            "end_ms": int(segment["end_ms"]),
            "hypothesis_text_at_export": segment["text_display"] or "",
            "hypothesis_speaker_at_export": segment["speaker_session_id"]
            or segment["person_name"]
            or "unknown",
            "audio_ref": segment["audio_ref"],
            "reference_text": "",
            "reference_speaker": "",
            "reference_identity": "",
            "key_facts": [],
            "notes": "",
        }
        for segment in segments
    ]
    truth_path.write_text(
        "\n".join(
            [json.dumps(metadata, ensure_ascii=False)]
            + [json.dumps(row, ensure_ascii=False) for row in rows]
        )
        + "\n",
        encoding="utf-8",
    )
    instructions_path.write_text(
        _render_template_instructions(metadata, truth_path), encoding="utf-8"
    )
    return EvaluationTemplateSummary(
        recording_id=recording_id,
        item_count=len(rows),
        speech_seconds=sum(row["end_ms"] - row["start_ms"] for row in rows) / 1000,
        truth_path=truth_path,
        instructions_path=instructions_path,
    )


def evaluate_truth(database: Database, truth_path: Path) -> EvaluationSummary:
    truth_path = truth_path.resolve(strict=True)
    metadata, truth_rows = _load_truth(truth_path)
    recording_id = int(metadata["recording_id"])
    database.get_recording(recording_id)
    segments = {
        int(segment["id"]): segment for segment in database.all_segments(recording_id)
    }
    self_profile = database.get_self_profile()
    self_profile_id = int(self_profile["id"]) if self_profile is not None else None

    text_totals = {"reference_chars": 0, "substitutions": 0, "deletions": 0, "insertions": 0}
    text_segments = 0
    exact_text = 0
    identity = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    speaker_rows: list[tuple[str, str]] = []
    key_fact_total = 0
    key_fact_matched = 0
    item_results: list[dict] = []

    for truth in truth_rows:
        if not truth.get("include", True):
            continue
        segment_id = int(truth["segment_id"])
        segment = segments.get(segment_id)
        if segment is None:
            raise ValueError(f"片段 {segment_id} 不属于录音 {recording_id}")
        hypothesis_text = segment["text_display"] or ""
        hypothesis_speaker = (
            segment["speaker_session_id"] or segment["person_name"] or "unknown"
        )
        result = {
            "segment_id": segment_id,
            "hypothesis_text": hypothesis_text,
            "reference_text": truth.get("reference_text", ""),
            "hypothesis_speaker": hypothesis_speaker,
            "reference_speaker": truth.get("reference_speaker", ""),
        }

        reference_text = str(truth.get("reference_text", "")).strip()
        if reference_text:
            reference_normalized = normalize_text(reference_text)
            hypothesis_normalized = normalize_text(hypothesis_text)
            operations = levenshtein_operations(reference_normalized, hypothesis_normalized)
            text_segments += 1
            exact_text += reference_normalized == hypothesis_normalized
            text_totals["reference_chars"] += len(reference_normalized)
            for key in ("substitutions", "deletions", "insertions"):
                text_totals[key] += operations[key]
            result["text_operations"] = operations

        reference_speaker = str(truth.get("reference_speaker", "")).strip()
        if reference_speaker:
            speaker_rows.append((reference_speaker, str(hypothesis_speaker)))

        reference_identity = str(truth.get("reference_identity", "")).strip().lower()
        predicted_self = (
            self_profile_id is not None and segment["person_id"] == self_profile_id
        )
        if reference_identity in {"self", "not_self"}:
            if reference_identity == "self" and predicted_self:
                identity["tp"] += 1
            elif reference_identity == "self":
                identity["fn"] += 1
            elif predicted_self:
                identity["fp"] += 1
            else:
                identity["tn"] += 1
            result["reference_identity"] = reference_identity
            result["predicted_identity"] = "self" if predicted_self else "not_self"

        key_facts = truth.get("key_facts", [])
        if not isinstance(key_facts, list):
            raise ValueError(f"片段 {segment_id} 的 key_facts 必须是列表")
        normalized_hypothesis = normalize_text(hypothesis_text)
        matched_facts: list[str] = []
        for fact in key_facts:
            fact_text = str(fact).strip()
            if not fact_text:
                continue
            key_fact_total += 1
            if normalize_text(fact_text) in normalized_hypothesis:
                key_fact_matched += 1
                matched_facts.append(fact_text)
        if key_facts:
            result["key_facts"] = key_facts
            result["matched_key_facts"] = matched_facts
        item_results.append(result)

    if not item_results:
        raise RuntimeError("真值文件中没有 include=true 的片段")

    total_errors = (
        text_totals["substitutions"]
        + text_totals["deletions"]
        + text_totals["insertions"]
    )
    speaker_metrics = _speaker_pair_metrics(speaker_rows)
    metrics = {
        "included_segments": len(item_results),
        "text": {
            "evaluated_segments": text_segments,
            **text_totals,
            "errors": total_errors,
            "cer": (
                total_errors / text_totals["reference_chars"]
                if text_totals["reference_chars"]
                else None
            ),
            "exact_match_rate": exact_text / text_segments if text_segments else None,
        },
        "speaker_pairwise": speaker_metrics,
        "self_identity": {
            **identity,
            "precision": _safe_ratio(identity["tp"], identity["tp"] + identity["fp"]),
            "recall": _safe_ratio(identity["tp"], identity["tp"] + identity["fn"]),
            "accuracy": _safe_ratio(
                identity["tp"] + identity["tn"], sum(identity.values())
            ),
        },
        "key_facts": {
            "total": key_fact_total,
            "matched": key_fact_matched,
            "recall": _safe_ratio(key_fact_matched, key_fact_total),
        },
    }
    evaluation_config = {
        "format": EVALUATION_FORMAT,
        "text_normalization": "NFKC + lowercase + remove whitespace/punctuation",
        "speaker_metric": "label-permutation-invariant pairwise same/different",
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = recording_output_dir(recording_id) / "evaluations"
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = truth_path.stem
    report_json_path = output_dir / f"{stem}-{timestamp}.json"
    report_markdown_path = output_dir / f"{stem}-{timestamp}.md"
    report_payload = {
        "format": "AllDayRecording evaluation report v1",
        "recording_id": recording_id,
        "truth_path": str(truth_path),
        "truth_sha256": _sha256_file(truth_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": evaluation_config,
        "metrics": metrics,
        "segments": item_results,
    }
    report_json_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_markdown_path.write_text(
        _render_evaluation_report(report_payload), encoding="utf-8"
    )
    run_id = database.record_evaluation_run(
        recording_id,
        truth_path=str(truth_path),
        truth_sha256=report_payload["truth_sha256"],
        config=evaluation_config,
        metrics=metrics,
        report_json_path=str(report_json_path),
        report_markdown_path=str(report_markdown_path),
    )
    return EvaluationSummary(
        evaluation_run_id=run_id,
        recording_id=recording_id,
        item_count=len(item_results),
        metrics=metrics,
        report_json_path=report_json_path,
        report_markdown_path=report_markdown_path,
    )


def parse_offset(value: str) -> int:
    text = value.strip()
    if not text:
        raise ValueError("时间偏移不能为空")
    if ":" not in text:
        seconds = float(text)
    else:
        parts = text.split(":")
        if len(parts) not in {2, 3}:
            raise ValueError(f"无法解析时间偏移：{value}")
        numbers = [float(part) for part in parts]
        if len(numbers) == 2:
            seconds = numbers[0] * 60 + numbers[1]
        else:
            seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if seconds < 0:
        raise ValueError("时间偏移不能为负数")
    return round(seconds * 1000)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def levenshtein_operations(reference: str, hypothesis: str) -> dict[str, int]:
    previous = [(index, 0, 0, index) for index in range(len(hypothesis) + 1)]
    for ref_index, reference_character in enumerate(reference, start=1):
        current = [(ref_index, 0, ref_index, 0)]
        for hyp_index, hypothesis_character in enumerate(hypothesis, start=1):
            if reference_character == hypothesis_character:
                substitution = previous[hyp_index - 1]
            else:
                distance, substitutions, deletions, insertions = previous[hyp_index - 1]
                substitution = (
                    distance + 1,
                    substitutions + 1,
                    deletions,
                    insertions,
                )
            distance, substitutions, deletions, insertions = previous[hyp_index]
            deletion = (distance + 1, substitutions, deletions + 1, insertions)
            distance, substitutions, deletions, insertions = current[hyp_index - 1]
            insertion = (distance + 1, substitutions, deletions, insertions + 1)
            current.append(min(substitution, deletion, insertion))
        previous = current
    distance, substitutions, deletions, insertions = previous[-1]
    return {
        "distance": distance,
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
    }


def _load_truth(path: Path) -> tuple[dict, list[dict]]:
    rows: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"真值文件第 {line_number} 行不是有效 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"真值文件第 {line_number} 行必须是对象")
        rows.append(value)
    if not rows or rows[0].get("type") != "metadata":
        raise ValueError("真值文件第一行必须是 metadata")
    metadata = rows[0]
    if metadata.get("format") != EVALUATION_FORMAT:
        raise ValueError(f"不支持的真值格式：{metadata.get('format')}")
    segments = [row for row in rows[1:] if row.get("type") == "segment"]
    segment_ids = [int(row["segment_id"]) for row in segments]
    if len(segment_ids) != len(set(segment_ids)):
        raise ValueError("真值文件包含重复 segment_id")
    return metadata, segments


def _speaker_pair_metrics(rows: list[tuple[str, str]]) -> dict:
    tp = fp = fn = tn = 0
    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            reference_same = rows[left][0] == rows[right][0]
            hypothesis_same = rows[left][1] == rows[right][1]
            if reference_same and hypothesis_same:
                tp += 1
            elif reference_same:
                fn += 1
            elif hypothesis_same:
                fp += 1
            else:
                tn += 1
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    return {
        "evaluated_segments": len(rows),
        "evaluated_pairs": tp + fp + fn + tn,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall
            else None
        ),
        "pair_accuracy": _safe_ratio(tp + tn, tp + fp + fn + tn),
    }


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_template_instructions(metadata: dict, truth_path: Path) -> str:
    return f"""# 评测真值：{metadata['name']}

真值文件：`{truth_path}`

逐行编辑 `type=segment` 的字段：

- `reference_text`：人工听写的准确文字；留空则不计算 CER。
- `reference_speaker`：同一人物始终使用同一个标签，例如 `self`、`mother`、`teacher`、`tv`；用于不依赖标签名称的成对说话人指标。
- `reference_identity`：只在确认时填写 `self` 或 `not_self`；`tv`、`mixed`、`uncertain` 可保留在 notes，不参与本人二分类。
- `key_facts`：必须被正确识别的时间、地点、人名或动作，例如 `["明天十点", "饭店"]`。
- `include`：不适合评测的混合/损坏片段可改为 `false`。

需要单独回听某一片段时运行 `allday-asr clip <segment_id>`；`audio_ref` 记录了原始文件和时间范围。

不要修改 `segment_id`、`start_ms` 和 `end_ms`。完成后运行：

```powershell
allday-asr evaluation run "{truth_path}"
```
"""


def _render_evaluation_report(payload: dict) -> str:
    metrics = payload["metrics"]
    text = metrics["text"]
    speaker = metrics["speaker_pairwise"]
    identity = metrics["self_identity"]
    facts = metrics["key_facts"]

    def display(value) -> str:
        if value is None:
            return "N/A"
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    return "\n".join(
        [
            f"# Recording {payload['recording_id']} 评测报告",
            "",
            f"- 真值：`{payload['truth_path']}`",
            f"- 真值 SHA-256：`{payload['truth_sha256']}`",
            f"- 纳入片段：{metrics['included_segments']}",
            "",
            "## 核心指标",
            "",
            "| 指标 | 结果 |",
            "| --- | ---: |",
            f"| CER | {display(text['cer'])} |",
            f"| 文本完全匹配率 | {display(text['exact_match_rate'])} |",
            f"| 说话人成对 F1 | {display(speaker['f1'])} |",
            f"| 说话人成对准确率 | {display(speaker['pair_accuracy'])} |",
            f"| 本人识别 precision | {display(identity['precision'])} |",
            f"| 本人识别 recall | {display(identity['recall'])} |",
            f"| 本人识别 accuracy | {display(identity['accuracy'])} |",
            f"| 关键事实召回率 | {display(facts['recall'])} |",
            "",
            "没有填写相应真值的指标显示为 N/A，不会被当成 0。",
            "",
        ]
    )
