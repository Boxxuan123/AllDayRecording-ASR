from __future__ import annotations

import base64
from pathlib import Path
from uuid import uuid4

from allday_asr.v3.adapters.audio.tools import AudioClip, assemble_audio_clips
from .device_reviews import DeviceReviewService, MAX_REVIEW_AUDIO_BYTES, _required_text


class DeviceAnnotationService:
    def __init__(self, core):
        self.core = core

    def execute(self, device_id, payload):
        action = _required_text(payload, "action")
        if action == "status":
            return self.core.people.annotation_status(payload.get("utterance_ids"))
        if action == "retry_samples":
            ids = payload.get("utterance_ids")
            self.core.people.annotation_status(ids)  # validate the entire batch before writing
            self.core.people.process_annotation_samples([{"utterance_id": uid} for uid in ids])
            return self.core.people.annotation_status(ids)
        if action == "people":
            return {
                "people": [
                    {"person_id": p["person_id"], "display_name": p["display_name"]}
                    for p in self.core.people.list_people()
                ]
            }
        if action == "assign":
            selections = payload.get("selections")
            if not isinstance(selections, list):
                raise ValueError("请选择需要关联的发言")
            person_id = payload.get("person_id")
            display_name = payload.get("display_name")
            if person_id is not None:
                person_id = _required_text(payload, "person_id")
            if display_name is not None:
                display_name = _required_text(payload, "display_name")
            result = self.core.people.assign_utterances(
                selections,
                person_id=person_id,
                display_name=display_name,
                actor=f"phone-device:{device_id}",
            )
            result["reviews"] = DeviceReviewService(self.core).snapshot()
            return result
        if action != "audio":
            raise ValueError("unsupported annotation action")
        session_id = _required_text(payload, "session_id")
        start, end = payload.get("start_ms"), payload.get("end_ms")
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or not start < end <= start + 300_000
        ):
            raise ValueError("试听范围应为 0 至 5 分钟")
        descriptors = self.core.desktop.session_audio_clips(session_id, start, end)
        clips = tuple(
            AudioClip(
                self.core.audio_store.path_for(str(d["storage_key"])),
                int(d["start_ms"]),
                int(d["end_ms"]),
            )
            for d in descriptors
        )
        output = (
            Path(self.core.audio_store.root).parent
            / "review-audio-temp"
            / f"{uuid4().hex}.wav"
        )
        try:
            assemble_audio_clips(clips, output)
            if output.stat().st_size > MAX_REVIEW_AUDIO_BYTES:
                raise ValueError("音频过大，请缩短试听范围")
            data = output.read_bytes()
        finally:
            output.unlink(missing_ok=True)
        return {
            "audio": {
                "review_id": session_id,
                "prototype_id": f"{session_id}-{start}-{end}",
                "format": "wav",
                "start_ms": 0,
                "end_ms": end - start,
                "data_base64url": base64.urlsafe_b64encode(data)
                .rstrip(b"=")
                .decode("ascii"),
            }
        }
