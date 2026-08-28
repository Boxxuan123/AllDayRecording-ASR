from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from allday_asr.paths import recording_output_dir
from allday_asr.storage.database import Database


REVIEW_HEADER = re.compile(
    r"^##\s+\d+\.\s+segment\s+(?P<segment_id>\d+).*?（(?P<label>[^）]+)）\s*$",
    re.MULTILINE,
)
LABEL_MAP = {
    "我": "self",
    "我妈": "mother",
    "我爸": "father",
    "电视": "tv",
    "我+我妈": "mixed",
    "我+我爸": "mixed",
    "太短，听不出来": "uncertain",
}


@dataclass(frozen=True)
class ReviewImportSummary:
    recording_id: int
    annotations: int
    self_confirmed: int
    negative_confirmed: int
    mixed: int
    uncertain: int
    manual_assigned: int
    automatic_assigned: int
    threshold: float
    annotations_path: Path
    calibration_path: Path


def parse_review_markdown(text: str) -> list[dict]:
    matches = list(REVIEW_HEADER.finditer(text))
    annotations: list[dict] = []
    for index, match in enumerate(matches):
        raw_label = match.group("label").strip()
        identity_label = LABEL_MAP.get(raw_label)
        if identity_label is None:
            continue
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.end() : section_end]
        note_match = re.search(r"^-\s*修订：(.+)$", section, re.MULTILINE)
        annotations.append(
            {
                "segment_id": int(match.group("segment_id")),
                "identity_label": identity_label,
                "raw_label": raw_label,
                "confidence": "uncertain" if identity_label == "uncertain" else "confirmed",
                "annotation_source": "human",
                "note": note_match.group(1).strip() if note_match else None,
            }
        )
    return annotations


def import_self_review(
    database: Database,
    recording_id: int,
    *,
    review_path: Path | None = None,
    threshold: float = 0.36,
) -> ReviewImportSummary:
    if not 0 < threshold <= 1:
        raise ValueError("threshold 必须在 0 到 1 之间")
    root = recording_output_dir(recording_id) / "self-candidates"
    review_path = review_path or root / "README.md"
    scores_path = root / "scores.json"
    if not review_path.is_file() or not scores_path.is_file():
        raise RuntimeError("找不到候选 README 或 scores.json")
    annotations = parse_review_markdown(review_path.read_text(encoding="utf-8"))
    if not annotations:
        raise RuntimeError("README 中没有识别到人工标注")
    score_payload = json.loads(scores_path.read_text(encoding="utf-8"))
    score_rows = {
        int(item["segment_id"]): item for item in score_payload.get("segments", [])
    }
    for annotation in annotations:
        row = score_rows.get(annotation["segment_id"])
        if row is None:
            raise RuntimeError(f"标注片段 {annotation['segment_id']} 不在 scores.json 中")
        annotation.update(
            {
                "score": float(row["score_min"]),
                "duration_ms": int(row["duration_ms"]),
                "text": row.get("text", ""),
            }
        )

    profile = database.get_self_profile()
    if profile is None:
        raise RuntimeError("尚未登记本人声纹，请先执行 enroll-self")
    database.upsert_segment_annotations(recording_id, annotations)
    profile_id = int(profile["id"])
    database.clear_person_assignments(recording_id, profile_id)

    annotation_by_segment = {item["segment_id"]: item for item in annotations}
    manual_ids = {
        item["segment_id"]
        for item in annotations
        if item["identity_label"] == "self" and item["confidence"] == "confirmed"
    }
    automatic_ids = {
        segment_id
        for segment_id, row in score_rows.items()
        if float(row["score_min"]) >= threshold and segment_id not in annotation_by_segment
    }
    manual_scores = [
        (segment_id, float(score_rows[segment_id]["score_min"]))
        for segment_id in sorted(manual_ids)
    ]
    automatic_scores = [
        (segment_id, float(score_rows[segment_id]["score_min"]))
        for segment_id in sorted(automatic_ids)
    ]
    manual_assigned = database.assign_person_to_segments(
        recording_id, profile_id, manual_scores
    )
    automatic_assigned = database.assign_person_to_segments(
        recording_id, profile_id, automatic_scores
    )

    positives = [item for item in annotations if item["identity_label"] == "self"]
    negatives = [
        item
        for item in annotations
        if item["identity_label"] not in {"self", "mixed", "uncertain"}
    ]
    true_positive = sum(item["score"] >= threshold for item in positives)
    false_positive = sum(item["score"] >= threshold for item in negatives)
    calibration = {
        "format": "AllDayRecording self verification calibration v1",
        "recording_id": recording_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "threshold": threshold,
        "positive_samples": len(positives),
        "negative_samples": len(negatives),
        "true_positive": true_positive,
        "false_negative": len(positives) - true_positive,
        "false_positive": false_positive,
        "true_negative": len(negatives) - false_positive,
        "precision_on_reviewed": (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else None
        ),
        "recall_on_reviewed": true_positive / len(positives) if positives else None,
        "manual_assignment_segment_ids": sorted(manual_ids),
        "automatic_assignment_segment_ids": sorted(automatic_ids),
        "excluded_mixed_segment_ids": sorted(
            item["segment_id"] for item in annotations if item["identity_label"] == "mixed"
        ),
        "excluded_uncertain_segment_ids": sorted(
            item["segment_id"]
            for item in annotations
            if item["identity_label"] == "uncertain"
        ),
    }
    annotations_payload = {
        "format": "AllDayRecording human identity annotations v1",
        "recording_id": recording_id,
        "source_review": str(review_path.resolve()),
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "annotations": annotations,
    }
    annotations_path = root / "annotations.json"
    calibration_path = root / "calibration.json"
    annotations_path.write_text(
        json.dumps(annotations_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    calibration_path.write_text(
        json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ReviewImportSummary(
        recording_id=recording_id,
        annotations=len(annotations),
        self_confirmed=len(positives),
        negative_confirmed=len(negatives),
        mixed=sum(item["identity_label"] == "mixed" for item in annotations),
        uncertain=sum(item["identity_label"] == "uncertain" for item in annotations),
        manual_assigned=manual_assigned,
        automatic_assigned=automatic_assigned,
        threshold=threshold,
        annotations_path=annotations_path,
        calibration_path=calibration_path,
    )
