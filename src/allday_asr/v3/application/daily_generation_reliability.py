"""Exact accepted checkpoints; no semantic policy or validator decisions."""

from allday_asr.v3.domain.daily_protocol import (
    CANONICALIZATION_VERSION,
    canonicalize_response,
)
from allday_asr.v3.domain.hashing import canonical_json_sha256

GENERATION_POLICY_VERSION = "daily-semantic-v1.2-product-v1.2.1"


def request_fingerprint(analyzer, request, sources, prompt_version, schema_version):
    return canonical_json_sha256(
        {
            "model": analyzer.model_label,
            "provider": getattr(analyzer, "provider", "anonymous"),
            "reasoning": getattr(analyzer, "reasoning_effort", "high"),
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "generation_policy_version": GENERATION_POLICY_VERSION,
            "producer_version": analyzer.producer_version,
            # Includes text, metadata, ordering, candidates, tasks, recent/open and
            # lookahead context. Source identity/revision also covers unsampled rows.
            "request": {k: v for k, v in request.items() if k != "validation_feedback"},
            "sources": sources,
        }
    )


def canonical_result(analyzer, request, payload):
    validate = getattr(analyzer, "validate_cached_response", None)
    if callable(validate):
        validate(request, payload)
    value, audit = canonicalize_response(payload)
    if audit:
        record = getattr(analyzer, "record_canonicalization", None)
        if callable(record):
            record(audit)
    return value


def cache_hit(analyzer, digest):
    observer = getattr(analyzer, "record_cache_hit", None)
    if callable(observer):
        observer(digest)


def checkpoint_valid(cached, digest, field):
    provenance = cached.get("provenance", {})
    return provenance.get("input_sha256") == digest and provenance.get(
        "accepted_payload_sha256"
    ) == canonical_json_sha256(cached.get(field))


def seal_checkpoint(provenance, payload):
    return {**provenance, "accepted_payload_sha256": canonical_json_sha256(payload)}


def retry_allowed(exc):
    # Only protocol/schema rejection and recoverable provider errors. Runtime
    # safety failures are never retried and never converted into success.
    return isinstance(exc, (ValueError, TimeoutError, ConnectionError)) or any(
        word in str(exc).lower()
        for word in (
            "capacity",
            "timeout",
            "transport",
            "connection",
            "overloaded",
            "temporarily",
            "rate limit",
        )
    )
