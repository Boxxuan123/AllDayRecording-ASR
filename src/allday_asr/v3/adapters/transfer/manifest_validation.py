"""Validate the Phone recording manifest before publishing any metadata."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from allday_asr.v3.interfaces.transfer.store import UploadStoreError

_MANIFEST_FORMAT = "AllDayRecording session manifest v2"
_MAX_CHUNKS = 50_000


def _timezone_identity(value: object) -> object:
    # HarmonyOS reports CST for the same local zone represented by
    # Asia/Singapore in earlier manifests and the V3 workflow.
    return "Asia/Singapore" if value == "CST" else value


def _parse_manifest(payload: object) -> dict[str, Any]:
    required = {
        "format",
        "sessionKey",
        "sessionStartedAt",
        "device",
        "timezone",
        "audio",
        "chunks",
        "completedSegments",
        "totalSamples",
        "continuityValid",
    }
    if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - {"completion"}:
        raise UploadStoreError("V3 会话清单字段不符合协议")
    if payload["format"] != _MANIFEST_FORMAT:
        raise UploadStoreError("V3 会话清单版本不受支持")
    for key in ("sessionKey", "device", "timezone"):
        value = payload[key]
        if not isinstance(value, str) or not value or len(value) > 160:
            raise UploadStoreError(f"V3 会话清单 {key} 无效")
    for key in ("sessionStartedAt", "completedSegments", "totalSamples"):
        _nonnegative_int(payload[key], key)
    if payload["sessionStartedAt"] <= 0 or payload["totalSamples"] <= 0:
        raise UploadStoreError("V3 会话时间和样本总数必须为正数")
    if not isinstance(payload["continuityValid"], bool):
        raise UploadStoreError("V3 continuityValid 必须是布尔值")
    completion = payload.get("completion")
    if "completion" in payload and (not isinstance(completion, dict) or set(completion) != {
        "source", "completedSegments", "totalSamples", "confirmedAt"
    }):
        raise UploadStoreError("V3 结束证明格式无效")
    if completion is not None:
        if not isinstance(completion["source"], str) or completion["source"] not in {
            "watch_stop", "legacy_user_confirmed"
        }:
            raise UploadStoreError("V3 结束证明来源无效")
        for key in ("completedSegments", "totalSamples", "confirmedAt"):
            _nonnegative_int(completion[key], f"completion.{key}")
        if completion["confirmedAt"] <= 0 or (
            completion["completedSegments"] != payload["completedSegments"]
            or completion["totalSamples"] != payload["totalSamples"]
        ):
            raise UploadStoreError("V3 结束证明与当前音频清单不一致")
    audio = payload["audio"]
    if not isinstance(audio, dict) or set(audio) != {
        "sampleRate",
        "channels",
        "bitsPerSample",
    }:
        raise UploadStoreError("V3 audio 字段无效")
    for key in ("sampleRate", "channels", "bitsPerSample"):
        _nonnegative_int(audio[key], key)
        if audio[key] <= 0:
            raise UploadStoreError(f"V3 audio.{key} 必须为正数")
    if audio["bitsPerSample"] % 8 != 0:
        raise UploadStoreError("V3 bitsPerSample 必须按整字节对齐")
    chunks = payload["chunks"]
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= _MAX_CHUNKS:
        raise UploadStoreError("V3 chunks 数量无效")
    if payload["completedSegments"] != len(chunks):
        raise UploadStoreError("V3 completedSegments 与 chunks 不一致")
    seen_indexes: set[int] = set()
    highest_sample = 0
    previous_end = 0
    previous_index: int | None = None
    computed_continuity = True
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {
            "index",
            "fileName",
            "firstSample",
            "sampleCount",
        }:
            raise UploadStoreError("V3 chunk 字段无效")
        for key in ("index", "firstSample", "sampleCount"):
            _nonnegative_int(chunk[key], key)
        name = chunk["fileName"]
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or PurePosixPath(name).name != name
            or "\\" in name
            or not name.lower().endswith(".wav")
        ):
            raise UploadStoreError("V3 chunk fileName 必须是 WAV 基础文件名")
        if chunk["index"] in seen_indexes or chunk["sampleCount"] <= 0:
            raise UploadStoreError("V3 chunk index 重复或 sampleCount 无效")
        if chunk["firstSample"] != previous_end:
            computed_continuity = False
        if previous_index is not None and chunk["index"] != previous_index + 1:
            computed_continuity = False
        seen_indexes.add(chunk["index"])
        previous_end = chunk["firstSample"] + chunk["sampleCount"]
        previous_index = chunk["index"]
        highest_sample = max(
            highest_sample, chunk["firstSample"] + chunk["sampleCount"]
        )
    if highest_sample != payload["totalSamples"]:
        raise UploadStoreError("V3 totalSamples 与 chunks 不一致")
    if not computed_continuity or not payload["continuityValid"]:
        raise UploadStoreError("V3 结束清单中的分片不连续")
    return payload


def _nonnegative_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UploadStoreError(f"V3 {label} 必须是非负整数")
