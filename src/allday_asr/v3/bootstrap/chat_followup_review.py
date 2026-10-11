"""Visible review of saved model candidates using the existing personal admission."""

import json
import re
from allday_asr.v3.domain.chat_admission import evidence_digest
from allday_asr.v3.domain.chat_followups import validate_candidate
from allday_asr.v3.domain.chat_identity import frozen_record
from allday_asr.v3.ports.chat_data import ChatDataError


def load(controller, job_id, batch, candidate):
    if (
        not isinstance(job_id, str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", job_id)
        or type(batch) is not int
        or batch < 0
        or type(candidate) is not int
        or candidate < 0
    ):
        raise ChatDataError("INVALID_FOLLOWUP_REVIEW", 400)
    try:
        job = controller.service.job(job_id)
        saved = json.loads(
            (controller.root / f"{job_id}-{batch}.response.json").read_text(
                encoding="utf-8"
            )
        )
        proposal = saved["response"]["items"][candidate]
    except (KeyError, IndexError, FileNotFoundError):
        raise ChatDataError("FOLLOWUP_CANDIDATE_NOT_FOUND", 404) from None
    dataset = job["dataset"]
    if controller.chat.cache.get("dataset") != dataset:
        raise ChatDataError("FOLLOWUP_SOURCE_CHANGED", 409)
    refs = {r["record_id"] for r in proposal["evidence"]} | {proposal["anchor_id"]}
    originals = {r["record_id"]: r for r in saved["source_records"]}
    records = []
    for key in refs:
        original = originals.get(key)
        current = controller.chat.cache.record(key)
        if (
            not original
            or not current
            or frozen_record(current) != frozen_record(original)
        ):
            raise ChatDataError("FOLLOWUP_EVIDENCE_CHANGED", 409)
        records.append(current)
    links = controller.links()
    value = validate_candidate(
        proposal,
        records,
        dataset,
        links,
        int(controller.service.now().timestamp()),
        job.get("timezone"),
    )
    binding = {k: value[k] for k in ("source_key", "dataset", "conversation_key")}
    binding["evidence_digest"] = evidence_digest(value)
    return proposal, records, job, links, value, binding


def candidates(controller):
    result = []
    for job in controller.service.jobs(None):
        for path in sorted(controller.root.glob(job["id"] + "-*.response.json")):
            batch = -1
            try:
                batch = int(path.name[len(job["id"]) + 1 :].split(".")[0])
                saved = json.loads(path.read_text(encoding="utf-8"))
                proposals = saved["response"]["items"]
                if not isinstance(proposals, list):
                    raise ValueError("invalid saved candidate list")
            except (OSError, ValueError, KeyError, TypeError):
                result.append(
                    {
                        "job_id": job["id"],
                        "batch": batch,
                        "candidate": -1,
                        "title": "响应无法读取",
                        "error": "FOLLOWUP_RESPONSE_UNAVAILABLE",
                        "can_accept": False,
                        "accepted": False,
                        "binding": None,
                        "evidence": [],
                        "time": {},
                        "uncertainty": [],
                    }
                )
                continue
            for index, _proposal in enumerate(proposals):
                try:
                    _, _, _, _, value, binding = load(
                        controller, job["id"], batch, index
                    )
                    with controller.service.uow_factory().reading() as uow:
                        admission = uow.followups.admission(
                            value["source_key"], job["dataset"]
                        )
                        source = uow.followups.source(value["source_key"])
                        if admission and admission["origin"] not in {
                            "rule",
                            "human_selection",
                        }:
                            continue
                        accepted = bool(
                            source
                            and (
                                not admission
                                or admission["decision"] in {"task", "cared"}
                            )
                            and value["action"] == "create"
                        )
                        conflict = (
                            json.loads(source["conflict_json"])
                            if source and source["conflict_json"]
                            else None
                        )
                        pending = bool(
                            conflict
                            and evidence_digest(conflict) == binding["evidence_digest"]
                            and conflict["action"] == value["action"]
                        )
                        revision = None
                        if source:
                            current = uow.knowledge.get_event(source["event_id"])
                            revision = current.revision
                            if accepted:
                                value["title"] = current.payload.get(
                                    "title", value["title"]
                                )
                                chat = current.payload.get("chat_followup", {})
                                value["category"] = (
                                    "ended"
                                    if current.status.value != "active"
                                    or source["ignored"]
                                    else chat.get("category", "pending")
                                )
                                if (
                                    chat.get("historical")
                                    and value["category"] != "ended"
                                ):
                                    value["category"] = "pending"
                    result.append(
                        {
                            "job_id": job["id"],
                            "batch": batch,
                            "candidate": index,
                            "binding": binding,
                            "title": value["title"],
                            "actor_is_from_self": value["actor_is_from_self"],
                            "category": value["category"],
                            "time": value["time"],
                            "uncertainty": value.get("uncertainty", []),
                            "accepted": accepted,
                            "can_accept": not accepted and value["action"] == "create",
                            "change_pending": pending,
                            "change_kind": conflict.get("change_kind")
                            if pending
                            else None,
                            "revision": revision,
                            "action": value["action"],
                            "evidence": [
                                {
                                    k: r.get(k)
                                    for k in (
                                        "record_id",
                                        "quote",
                                        "is_from_self",
                                        "self_identity_basis",
                                    )
                                }
                                for r in value["evidence"]
                            ],
                        }
                    )
                except (
                    ChatDataError,
                    KeyError,
                    IndexError,
                    TypeError,
                    ValueError,
                ) as exc:
                    result.append(
                        {
                            "job_id": job["id"],
                            "batch": batch,
                            "candidate": index,
                            "title": "候选证据不可用",
                            "error": exc.code
                            if isinstance(exc, ChatDataError)
                            else "INVALID_FOLLOWUP_CANDIDATE",
                            "can_accept": False,
                            "accepted": False,
                            "binding": None,
                            "evidence": [],
                            "time": {},
                            "uncertainty": [],
                        }
                    )
    return {"items": result}


def accept(controller, job_id, batch, candidate, binding):
    proposal, records, job, links, value, expected = load(
        controller, job_id, batch, candidate
    )
    if proposal["action"] != "create":
        raise ChatDataError("FOLLOWUP_PROGRESS_REQUIRES_REVIEW", 409)
    if binding != expected:
        raise ChatDataError("RELEVANCE_BINDING_CHANGED", 409)
    with controller.service.uow_factory().reading() as uow:
        source = uow.followups.source(value["source_key"])
        admission = uow.followups.admission(value["source_key"], job["dataset"])
        if (
            source
            and admission
            and admission["origin"] == "human_selection"
            and admission["decision"] == "task"
        ):
            return {"effect": "already_accepted"}
    reviewed = controller.service.review_relevance(
        proposal,
        records,
        job["dataset"],
        links,
        binding=expected,
        decision="task",
        origin="human_selection",
        review_ref="desktop-candidate:"
        + job_id
        + ":"
        + str(batch)
        + ":"
        + str(candidate),
        timezone_name=job.get("timezone"),
        reason="用户明确接纳为个人跟进；截止时间及完成状态的原不确定性保留",
    )
    if reviewed["effect"] != "reviewed":
        return reviewed
    applied = controller.service.apply(
        proposal, records, job["dataset"], links, timezone_name=job.get("timezone")
    )
    # Protect this explicit selection without using confirm to clear progress_unknown
    # or historical flags. No reminder creation or recording-event ID is involved.
    with controller.service.uow_factory() as uow:
        source = uow.followups.source(value["source_key"])
        if source:
            source["human_override"] = 1
            uow.followups.save_source(value["source_key"], source)
    return applied
