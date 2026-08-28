from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from allday_asr.paths import recording_output_dir
from allday_asr.storage.database import Database


DATE_PATTERN = re.compile(
    r"大后天|后天|明天|明早|明晚|今晚|今天|"
    r"(?:下周|本周|这周|周|星期)[一二三四五六日天]|"
    r"\d{1,2}月\d{1,2}[日号]"
)
CLOCK_PATTERN = re.compile(
    r"(?:(?P<period>上午|中午|下午|晚上|晚|早上|早晨|凌晨)\s*)?"
    r"(?P<hour>[0-2]?\d|[零〇一二两三四五六七八九十]{1,3})(?:点|时)"
    r"(?P<minute>半|[0-5]?\d分?|[零〇一二两三四五六七八九十]{1,3}分?)?"
)
COLON_CLOCK_PATTERN = re.compile(
    r"(?:(?P<period>上午|中午|下午|晚上|晚|早上|早晨|凌晨)\s*)?"
    r"(?P<hour>[0-2]?\d):(?P<minute>[0-5]\d)"
)
SCHEDULE_PATTERN = re.compile(r"见|碰面|集合|开会|上课|吃饭|聚餐|面试|约|出发|到达")
TODO_PATTERN = re.compile(r"记得|别忘|提醒|提交|交作业|发给|联系|打电话|买|取|拿|完成")
CONFIRM_PATTERN = re.compile(r"好的|好啊|好吧|可以|没问题|知道了|就这么定|约好了|行[啊吧呀]?$")


@dataclass(frozen=True)
class ActionExtractionSummary:
    recording_id: int
    detected_now: int
    total_candidates: int
    pending_candidates: int
    json_path: Path
    markdown_path: Path


def extract_action_candidates(
    database: Database,
    recording_id: int,
    *,
    require_self_confirmation: bool = True,
    confirmation_window_seconds: float = 90.0,
    min_confidence: float = 0.75,
) -> ActionExtractionSummary:
    if confirmation_window_seconds <= 0:
        raise ValueError("confirmation_window_seconds 必须大于 0")
    if not 0 < min_confidence <= 1:
        raise ValueError("min_confidence 必须在 0 到 1 之间")
    recording = database.get_recording(recording_id)
    segments = database.all_segments(recording_id, completed_only=True)
    self_profile = database.get_self_profile()
    self_profile_id = int(self_profile["id"]) if self_profile is not None else None
    confirmation_window_ms = round(confirmation_window_seconds * 1000)
    detected_now = 0

    for index, segment in enumerate(segments):
        text = str(segment["text_display"] or "").strip()
        date_match = DATE_PATTERN.search(text)
        clock_match = CLOCK_PATTERN.search(text) or COLON_CLOCK_PATTERN.search(text)
        if date_match is None or clock_match is None:
            continue
        candidate_type = _candidate_type(text)
        if candidate_type is None:
            continue

        proposal_is_self = (
            self_profile_id is not None and segment["person_id"] == self_profile_id
        )
        confirmation = _find_self_confirmation(
            segments,
            index,
            self_profile_id=self_profile_id,
            window_ms=confirmation_window_ms,
        )
        has_self_commitment = proposal_is_self or confirmation is not None
        if require_self_confirmation and not has_self_commitment:
            continue

        location = _extract_location(text, date_match, clock_match)
        confidence = 0.65
        if has_self_commitment:
            confidence += 0.2
        if location:
            confidence += 0.1
        confidence = min(confidence, 0.98)
        if confidence < min_confidence:
            continue

        source_segments = [segment]
        if confirmation is not None and int(confirmation["id"]) != int(segment["id"]):
            source_segments.append(confirmation)
        source_ids = [int(item["id"]) for item in source_segments]
        time_text = text[
            min(date_match.start(), clock_match.start()) : max(
                date_match.end(), clock_match.end()
            )
        ]
        scheduled_at = _resolve_scheduled_at(
            recording,
            int(segment["start_ms"]),
            date_match.group(0),
            clock_match,
        )
        participants = sorted(
            {
                str(item["person_name"] or item["speaker_session_id"])
                for item in source_segments
                if item["person_name"] or item["speaker_session_id"]
            }
        )
        evidence = {
            "proposal": _segment_evidence(segment),
            "confirmation": _segment_evidence(confirmation) if confirmation else None,
            "self_commitment": has_self_commitment,
            "rule_version": 1,
        }
        candidate_key = hashlib.sha256(
            f"{recording_id}:{candidate_type}:{source_ids}:{text}".encode("utf-8")
        ).hexdigest()
        database.upsert_action_candidate(
            {
                "recording_id": recording_id,
                "candidate_key": candidate_key,
                "candidate_type": candidate_type,
                "start_ms": int(segment["start_ms"]),
                "end_ms": int(source_segments[-1]["end_ms"]),
                "source_segment_ids": source_ids,
                "title": _candidate_title(text, candidate_type, location),
                "scheduled_at": scheduled_at,
                "time_text": time_text,
                "location": location,
                "participants": participants,
                "confidence": confidence,
                "evidence": evidence,
            }
        )
        detected_now += 1

    candidates = database.list_action_candidates(recording_id)
    pending = [candidate for candidate in candidates if candidate["status"] == "pending"]
    output_dir = recording_output_dir(recording_id)
    json_path = output_dir / "action-candidates.json"
    markdown_path = output_dir / "action-candidates.md"
    payload = {
        "format": "AllDayRecording action candidates v1",
        "recording_id": recording_id,
        "require_self_confirmation": require_self_confirmation,
        "confirmation_window_seconds": confirmation_window_seconds,
        "min_confidence": min_confidence,
        "candidates": [_candidate_payload(candidate) for candidate in candidates],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    database.set_stage(
        recording_id,
        "actions",
        "completed",
        details={
            "detected_now": detected_now,
            "total_candidates": len(candidates),
            "pending_candidates": len(pending),
            "require_self_confirmation": require_self_confirmation,
            "confirmation_window_seconds": confirmation_window_seconds,
            "min_confidence": min_confidence,
            "json_path": str(json_path),
            "markdown_path": str(markdown_path),
        },
    )
    return ActionExtractionSummary(
        recording_id=recording_id,
        detected_now=detected_now,
        total_candidates=len(candidates),
        pending_candidates=len(pending),
        json_path=json_path,
        markdown_path=markdown_path,
    )


def _candidate_type(text: str) -> str | None:
    if TODO_PATTERN.search(text):
        return "todo"
    if SCHEDULE_PATTERN.search(text):
        return "schedule"
    return None


def _find_self_confirmation(
    segments,
    proposal_index: int,
    *,
    self_profile_id: int | None,
    window_ms: int,
):
    if self_profile_id is None:
        return None
    proposal = segments[proposal_index]
    proposal_end = int(proposal["end_ms"])
    for candidate in segments[proposal_index + 1 : proposal_index + 5]:
        if int(candidate["start_ms"]) - proposal_end > window_ms:
            break
        if candidate["person_id"] != self_profile_id:
            continue
        text = re.sub(r"[\s，。！？,.!?]", "", str(candidate["text_display"] or ""))
        if CONFIRM_PATTERN.search(text):
            return candidate
    return None


def _extract_location(text: str, date_match: re.Match, clock_match: re.Match) -> str | None:
    cleaned = text
    for match in sorted((date_match, clock_match), key=lambda item: item.start(), reverse=True):
        cleaned = cleaned[: match.start()] + cleaned[match.end() :]
    cleaned = re.sub(r"[，。！？,.!?\s]", "", cleaned)
    patterns = (
        re.compile(r"(?:在|去|到)(?P<location>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:见|碰面|集合|吃饭|开会|上课)"),
        re.compile(r"(?P<location>[\u4e00-\u9fffA-Za-z0-9·]{2,8})(?:见|碰面|集合)"),
    )
    for pattern in patterns:
        match = pattern.search(cleaned)
        if match:
            return match.group("location").lstrip("在去到") or None
    return None


def _resolve_scheduled_at(
    recording,
    segment_start_ms: int,
    date_text: str,
    clock_match: re.Match,
) -> str | None:
    try:
        recorded_at = datetime.fromisoformat(str(recording["recorded_at"]).replace("Z", "+00:00"))
        local_zone = ZoneInfo(recording["timezone"])
    except (ValueError, ZoneInfoNotFoundError):
        return None
    uttered_at = (recorded_at + timedelta(milliseconds=segment_start_ms)).astimezone(
        local_zone
    )
    target_date = _resolve_date(uttered_at, date_text)
    if target_date is None:
        return None
    hour = _number_value(clock_match.group("hour"))
    minute_text = clock_match.group("minute")
    minute = (
        30
        if minute_text == "半"
        else _number_value((minute_text or "0").rstrip("分"))
    )
    period = clock_match.group("period")
    if period in {"下午", "晚上", "晚"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return datetime.combine(target_date, time(hour, minute), tzinfo=local_zone).isoformat()


def _number_value(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits[left] if left else 1
        ones = digits[right] if right else 0
        return tens * 10 + ones
    if len(value) == 1 and value in digits:
        return digits[value]
    raise ValueError(f"无法解析中文数字：{value}")


def _resolve_date(uttered_at: datetime, date_text: str):
    if date_text in {"今天", "今晚"}:
        return uttered_at.date()
    if date_text in {"明天", "明早", "明晚"}:
        return (uttered_at + timedelta(days=1)).date()
    if date_text == "后天":
        return (uttered_at + timedelta(days=2)).date()
    if date_text == "大后天":
        return (uttered_at + timedelta(days=3)).date()
    month_day = re.fullmatch(r"(\d{1,2})月(\d{1,2})[日号]", date_text)
    if month_day:
        month, day = map(int, month_day.groups())
        try:
            candidate = uttered_at.date().replace(month=month, day=day)
            if candidate < uttered_at.date():
                candidate = candidate.replace(year=candidate.year + 1)
            return candidate
        except ValueError:
            return None
    weekday_match = re.fullmatch(
        r"(?P<prefix>下周|本周|这周|周|星期)(?P<day>[一二三四五六日天])",
        date_text,
    )
    if weekday_match:
        target = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[
            weekday_match.group("day")
        ]
        prefix = weekday_match.group("prefix")
        if prefix in {"下周", "本周", "这周"}:
            week_start = uttered_at.date() - timedelta(days=uttered_at.weekday())
            week_offset = 7 if prefix == "下周" else 0
            return week_start + timedelta(days=week_offset + target)
        days_ahead = (target - uttered_at.weekday()) % 7
        return (uttered_at + timedelta(days=days_ahead)).date()
    return None


def _candidate_title(text: str, candidate_type: str, location: str | None) -> str:
    cleaned = text.strip(" ，。！？,.!?")
    if len(cleaned) <= 80:
        return cleaned
    prefix = "日程" if candidate_type == "schedule" else "待办"
    return f"{prefix}：{location or cleaned[:64]}"


def _segment_evidence(segment) -> dict | None:
    if segment is None:
        return None
    return {
        "segment_id": int(segment["id"]),
        "start_ms": int(segment["start_ms"]),
        "end_ms": int(segment["end_ms"]),
        "speaker": segment["person_name"]
        or segment["speaker_session_id"]
        or "unknown",
        "text": segment["text_display"] or "",
        "audio_ref": segment["audio_ref"],
    }


def _candidate_payload(candidate) -> dict:
    return {
        "id": int(candidate["id"]),
        "type": candidate["candidate_type"],
        "status": candidate["status"],
        "title": candidate["title"],
        "scheduled_at": candidate["scheduled_at"],
        "time_text": candidate["time_text"],
        "location": candidate["location"],
        "confidence": candidate["confidence"],
        "source_segment_ids": json.loads(candidate["source_segment_ids_json"]),
        "participants": json.loads(candidate["participants_json"]),
        "evidence": json.loads(candidate["evidence_json"]),
    }


def _render_markdown(payload: dict) -> str:
    lines = [
        f"# Recording {payload['recording_id']} 日程/待办候选",
        "",
        "> 这些内容只供确认；系统尚未写入任何真实日历或待办应用。",
        "",
    ]
    candidates = payload["candidates"]
    if not candidates:
        lines.append("没有检测到满足当前高置信规则的候选。")
        lines.append("")
        return "\n".join(lines)
    for candidate in candidates:
        lines.extend(
            [
                f"## #{candidate['id']} · {candidate['type']} · {candidate['status']}",
                "",
                f"- 标题：{candidate['title']}",
                f"- 时间：{candidate['scheduled_at'] or candidate['time_text'] or '未解析'}",
                f"- 地点：{candidate['location'] or '未解析'}",
                f"- 置信度：{candidate['confidence']:.2f}",
                f"- 证据片段：{candidate['source_segment_ids']}",
                "",
                "```powershell",
                f"allday-asr action-review {candidate['id']} --status confirmed",
                f"allday-asr action-review {candidate['id']} --status dismissed",
                "```",
                "",
            ]
        )
    return "\n".join(lines)
