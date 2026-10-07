"""Read-only source metadata inventory, without model calls or transcript loads."""

from datetime import datetime

from allday_asr.v3.domain.hashing import canonical_json_sha256

BACKFILL_CLASSES = {"LEGACY_ONLY", "MISSING", "STALE_SOURCE", "FAILED_RESUMABLE"}


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def generation_version(analyzer):
    from .daily_generation_reliability import GENERATION_POLICY_VERSION
    from allday_asr.v3.domain.daily_protocol import CANONICALIZATION_VERSION

    names = (
        "model_label",
        "provider",
        "producer_version",
        "reasoning_effort",
        "prompt_version",
        "extractor_version",
        "reconcile_prompt_version",
        "reconcile_schema_version",
        "normalization_prompt_version",
        "normalization_review_prompt_version",
        "normalization_scope_prompt_version",
        "overview_prompt_version",
        "overview_schema_version",
    )
    return canonical_json_sha256(
        {
            "policy": GENERATION_POLICY_VERSION,
            "canonicalization": CANONICALIZATION_VERSION,
            **{k: getattr(analyzer, k, None) for k in names},
        }
    )


def classify_day(item, job, *, current_product, source_changed, today):
    if not item["upstream_complete"]:
        return "UPSTREAM_INCOMPLETE", "Recording/ASR input is not complete"
    if not item["active_utterance_count"]:
        return "NO_SOURCE", "No eligible active utterances"
    if item["date"] >= today:
        return "STALE_SOURCE", "Current day stays dirty until local rollover"
    if job and job["status"] == "FAILED_TERMINAL":
        return "FAILED_TERMINAL", job.get(
            "error"
        ) or "Persistent retry budget exhausted"
    if job and job["status"] == "WAITING_PROVIDER":
        return (
            "UNKNOWN",
            "NOT_ATTEMPTED_PROVIDER_OUTAGE; held until explicit retry or source/version change",
        )
    if job and job["status"] in ("FAILED_RETRYABLE", "RUNNING", "PENDING"):
        return (
            "FAILED_RESUMABLE",
            "Persisted job/checkpoints; source identity unchanged",
        )
    if current_product and not source_changed:
        kind = (
            "CURRENT_COMPLETE"
            if item["existing_final_event_count"]
            else "CURRENT_COMPLETE_EMPTY"
        )
        return (
            kind,
            "Complete approved product, source metadata and published revisions match",
        )
    if current_product:
        return "STALE_SOURCE", "Source fingerprint/revision changed after publication"
    if item["existing_summary_present"] or item["existing_final_event_count"]:
        return (
            "LEGACY_ONLY",
            "Published row is not an approved complete semantic generation",
        )
    return "MISSING", "Eligible sources have no approved published generation"
