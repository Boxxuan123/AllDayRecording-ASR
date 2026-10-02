"""Infer self on a non-learning recording without creating voice samples."""

import json
import logging
from dataclasses import asdict

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.product_self_gate import product_self_gate
from .durable_processing import CorrectUtteranceCommand, apply_utterance_correction
from .people_support import _datetime


def infer_product_self(service, session_id):
    matcher = service._self_identity_matcher
    degraded = False
    try:
        status = matcher.status() if matcher is not None else {"auto_identity_enabled": False}
    except Exception:
        logging.getLogger(__name__).exception('Self matcher status unavailable')
        status = {"auto_identity_enabled": False, "reason": "matcher_status_exception"}
        degraded = True
    with service._uow_factory().reading() as uow:
        queries = uow.people.product_self_queries(session_id, service._artifact_root)
        person_id = uow.people.self_person_id()
    available = bool(person_id and status.get("auto_identity_enabled"))
    tracks = tuple(t for q in queries for t in q["tracks"]) if available else ()
    try:
        embeddings = {e.speaker_track_id: e for e in service._provider.embed(tracks)}
    except Exception:
        logging.getLogger(__name__).exception('Speaker embedding failed; identity remains Unknown')
        embeddings = {}
        degraded = True
    traces = []
    for query in queries:
        decisions = []
        invoked = False
        reason = query["reason"]
        if not available:
            reason = status.get("reason", "self_enrollment_or_person_unavailable")
        elif reason is None:
            for track in query["tracks"]:
                embedding = embeddings.get(track.speaker_track_id)
                duration = sum(c.source_end_ms-c.source_start_ms for c in track.clips)
                if embedding is None:
                    decisions.append({"decision": "unknown", "reason": "embedding_unavailable",
                                      "duration_ms": duration})
                else:
                    invoked = True
                    try:
                        decision = matcher.match(embedding).evidence
                    except Exception:
                        logging.getLogger(__name__).exception('Self matcher failed; window remains Unknown')
                        decision = {"decision": "unknown", "reason": "matcher_exception"}
                        degraded = True
                    decisions.append({**decision, "duration_ms": duration})
        # Existing V3.1 window requirement: two disjoint >=2s windows, all agree.
        identity, reason, eligible_count = product_self_gate(decisions, reason)
        traces.append({k: v for k, v in query.items() if k != "tracks"} | {
            "query_inputs": [asdict(t) for t in query["tracks"]],
            "self_matcher_invoked": invoked, "open_set_matcher": "SKIPPED:self-only recovery",
            "window_decisions": decisions, "eligible_window_count": eligible_count,
            "decision_before_projection": identity.value, "reason": reason,
            "cluster_assignment": "SKIPPED:inference does not create profiles or clusters"})
    run_id = new_ulid()
    updated = 0
    with service._uow_factory() as uow:
        for trace in traces:
            utterance = uow.evidence.get_utterance(trace["utterance_id"])
            if utterance.revision != trace["revision"] or utterance.status != "active":
                raise ValueError("product identity inputs changed; retry with a fresh snapshot")
            identity = SelfIdentity(trace["decision_before_projection"])
            history = uow.corrections.list_for_target("utterance", utterance.utterance_id)
            manual = any("identity" in c.patch and not c.actor.startswith("system:") for c in history)
            if (identity is not SelfIdentity.UNKNOWN and utterance.identity is SelfIdentity.UNKNOWN
                    and not manual and not utterance.evidence.get("person_annotation")):
                apply_utterance_correction(uow, CorrectUtteranceCommand(
                    utterance_id=utterance.utterance_id, expected_revision=utterance.revision,
                    text=utterance.text, actor="system:product-self-inference",
                    identity=identity, change_identity=True), service._now())
                updated += 1
            trace["final_pc_projection"] = uow.evidence.get_utterance(utterance.utterance_id).identity.value
            trace["manual_identity_preserved"] = manual
        now = _datetime(service._now())
        uow.people.connection.execute("""INSERT INTO speaker_cluster_runs
          (cluster_run_id,session_id,producer,model,model_version,policy_json,status,track_count,created_at,completed_at)
          VALUES (?,?,'product-self-inference',?,?,?,'succeeded',?,?,?)""",
          (run_id,session_id,service._provider.model,service._provider.model_version,
           json.dumps({"purpose": "product_inference_only", "self_enrollment": status,
                       "profile_learning": False, "traces": traces}), len(tracks),now,now))
    return {"product_identity_run_id": run_id, "product_inference_executed": True,
            "product_inference_degraded": degraded,
            "product_self_updated_utterance_count": updated,
            "product_self_matched_utterance_count": sum(t["decision_before_projection"] == "self" for t in traces)}
