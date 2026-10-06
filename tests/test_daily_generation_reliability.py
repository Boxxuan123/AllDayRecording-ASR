"""Anonymous equivalence, negative provenance, and restart checkpoints."""

import copy
from types import SimpleNamespace
import pytest
from allday_asr.v3.domain.daily_protocol import (
    canonicalize_response,
    NonCanonicalizableProtocolError,
)
from allday_asr.v3.domain.daily_semantics import validate_segments
from allday_asr.v3.domain.daily_goal_reconcile import validate_groups
from allday_asr.v3.application.daily_semantic_analysis import analyze_daily
from allday_asr.v3.application.daily_generation_reliability import (
    request_fingerprint,
    checkpoint_valid,
    seal_checkpoint,
)
from tests.test_daily_semantics import rows, segment, AnonymousAnalyzer
from tests import test_daily_semantics as patterns

from tests.test_daily_goal_reconcile import candidates, group
from tests.test_daily_v12_policy import grounded

source = patterns.source


def test_exact_roles_deduplicate_then_strict_validate():
    values = candidates()[:1]
    g, desc = grounded(values)
    g["topic_purity"]["source_roles"] *= 2
    original = copy.deepcopy(g)
    value, audit = canonicalize_response({"groups": [g]})
    assert g == original
    assert (
        audit["canonicalization_operations"][0]["operation"]
        == "DEDUP_EXACT_SOURCE_ROLE"
    )
    validate_groups(value["groups"], values, {}, desc, require_purity=True)


@pytest.mark.parametrize("field", ["role", "reason", "evidence_utterance_ids"])
def test_conflicting_roles_never_choose_an_assignment(field):
    g, _ = grounded(candidates()[:1])
    role = copy.deepcopy(g["topic_purity"]["source_roles"][0])
    role[field] = (
        "INCIDENTAL"
        if field == "role"
        else "different"
        if field == "reason"
        else ["other-source"]
    )
    g["topic_purity"]["source_roles"].append(role)
    with pytest.raises(NonCanonicalizableProtocolError):
        canonicalize_response({"groups": [g]})


def test_set_evidence_stable_but_outcome_sequence_preserved():
    s = segment(0, 1)
    s["claims"][0]["evidence_indices"] = [1, 1, 0, 1]
    s["explicit_outcome"] = {
        "kind": "decision",
        "quote": "anonymous",
        "evidence_indices": [0, 0],
    }
    value, audit = canonicalize_response({"segments": [s]})
    assert value["segments"][0]["claims"][0]["evidence_indices"] == [1, 0]
    assert value["segments"][0]["explicit_outcome"] == s["explicit_outcome"]
    assert audit


@pytest.mark.parametrize(
    "mutation", ["outside", "outcome_source", "invalid_task", "missing"]
)
def test_old_and_new_segment_validators_reject(mutation):
    data = rows(count=2)
    s = segment(0, 1)
    if mutation == "outside":
        s["claims"][0]["evidence_indices"] = [2]
    elif mutation == "outcome_source":
        s["explicit_outcome"] = {
            "kind": "decision",
            "quote": "anonymous",
            "evidence_indices": [2],
        }
    elif mutation == "invalid_task":
        data[0]["task_ids"] = ["authoritative-task"]
        s["classification"] = "FILLER"
    else:
        s["end_index"] = 0
    payload = {"segments": [s]}
    canonical, audit = canonicalize_response(payload)
    assert canonical == payload and audit is None
    for value in (payload, canonical):
        with pytest.raises(ValueError):
            validate_segments(copy.deepcopy(value["segments"]), data, set())


@pytest.mark.parametrize("mutation", ["missing", "overlap", "unknown"])
def test_old_and_new_group_validators_reject(mutation):
    values = candidates()
    g, desc = grounded(values)
    if mutation == "missing":
        g["candidate_indices"] = [0]
    elif mutation == "unknown":
        g["title_evidence_utterance_ids"] = ["unknown-source"]
    groups = [g, copy.deepcopy(g)] if mutation == "overlap" else [g]
    canonical, _ = canonicalize_response({"groups": groups})
    for result in (groups, canonical["groups"]):
        with pytest.raises(ValueError):
            validate_groups(result, values, {}, desc, require_purity=True)


def test_no_prose_classification_ranges_or_candidate_repair():
    s = segment(0, 1, classification="FILLER")
    value, audit = canonicalize_response({"segments": [s]})
    assert value == {"segments": [s]} and audit is None
    g = group([0, 0], ["source", "source"])
    value, _ = canonicalize_response({"groups": [g]})
    assert value["groups"][0]["candidate_indices"] == [0, 0]
    assert value["groups"][0]["title"] == g["title"]


def fingerprint(
    request=None, data=None, prompt="prompt.1", schema="schema.1", **changes
):
    a = SimpleNamespace(
        model_label="anonymous-model",
        provider="anonymous",
        producer_version="sdk.1",
        reasoning_effort="high",
        **changes,
    )
    return request_fingerprint(
        a,
        request or {"open_context": [], "candidates": []},
        data or rows(count=1),
        prompt,
        schema,
    )


def test_exact_fingerprint_is_deterministic():
    assert fingerprint() == fingerprint()


@pytest.mark.parametrize(
    "field",
    [
        "revision",
        "text",
        "source_id",
        "prompt",
        "schema",
        "context",
        "candidate",
        "model",
        "provider",
        "reasoning",
    ],
)
def test_every_fingerprint_change_misses(field):
    request = {"open_context": [], "candidates": []}
    data = rows(count=1)
    a = SimpleNamespace(
        model_label="anonymous-model",
        provider="anonymous",
        producer_version="sdk.1",
        reasoning_effort="high",
    )
    before = request_fingerprint(a, request, data, "prompt.1", "schema.1")
    prompt, schema = "prompt.1", "schema.1"
    if field == "revision":
        data[0]["revision"] += 1
    elif field == "text":
        data[0]["text"] += " changed"
    elif field == "source_id":
        data[0]["utterance_id"] = "other"
    elif field == "prompt":
        prompt = "prompt.2"
    elif field == "schema":
        schema = "schema.2"
    elif field == "context":
        request["open_context"] = [{"text": "other"}]
    elif field == "candidate":
        request["candidates"] = [{"index": 0}]
    elif field == "model":
        a.model_label = "other"
    elif field == "provider":
        a.provider = "other"
    else:
        a.reasoning_effort = "low"
    assert request_fingerprint(a, request, data, prompt, schema) != before


def test_failed_or_corrupted_checkpoint_is_not_a_hit():
    assert not checkpoint_valid({"result": []}, "digest", "result")
    cached = {
        "result": [],
        "provenance": seal_checkpoint({"input_sha256": "digest"}, []),
    }
    assert checkpoint_valid(cached, "digest", "result")
    cached["result"] = ["changed"]
    assert not checkpoint_valid(cached, "digest", "result")


def test_restart_resumes_only_accepted_prefix_and_reexecutes_failure(source):
    class Failing(AnonymousAnalyzer):
        def __init__(self, fail):
            self.starts = []
            self.fail = fail

        def analyze(self, request):
            key = request["utterances"][0]["start_at"]
            self.starts.append(key)
            if self.fail and len(self.starts) > 1:
                raise ValueError("protocol rejected")
            return super().analyze(request)

    service = source[1]
    data = rows(count=15)
    first = Failing(True)
    service._daily_analyzer = first
    with pytest.raises(ValueError):
        analyze_daily(service, data)
    assert len(first.starts) == 3  # prefix, failed request, exactly one retry
    second = Failing(False)
    service._daily_analyzer = second
    analyze_daily(service, data)
    assert first.starts[0] not in second.starts
    assert second.starts[0] == first.starts[1]


def test_schema_parse_precedes_canonicalization():
    from allday_asr.v3.adapters.codex.daily_semantic_schema import validate_output

    payload = {"segments": [segment(0, 0)]}
    payload["segments"][0]["claims"][0]["evidence_indices"] = [True, True]
    with pytest.raises(ValueError):
        validate_output(payload)


def test_existing_literal_outcome_sanitization_is_unchanged():
    s = segment(0, 0)
    s["explicit_outcome"] = {
        "kind": "decision",
        "quote": "invented",
        "evidence_indices": [0],
    }
    canonical, _ = canonicalize_response({"segments": [s]})
    before = validate_segments([copy.deepcopy(s)], rows(count=1), set())
    after = validate_segments(canonical["segments"], rows(count=1), set())
    assert before == after and after[0]["explicit_outcome"] is None


@pytest.mark.parametrize(
    "field", ["CANONICALIZATION_VERSION", "GENERATION_POLICY_VERSION"]
)
def test_policy_and_canonicalizer_versions_invalidate_cache(monkeypatch, field):
    import allday_asr.v3.application.daily_generation_reliability as reliability

    before = fingerprint()
    monkeypatch.setattr(reliability, field, "changed-version")
    assert fingerprint() != before


def test_owned_timeout_ends_only_its_assigned_child_job(tmp_path):
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows exclusive Job")
    from allday_asr.v3.adapters.codex.owned_attempt import OwnedAttempt

    attempt = OwnedAttempt(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        tmp_path / "job.log",
        tmp_path,
    )
    assert attempt.identity["assigned_before_any_child_or_model_call"]
    assert attempt.wait(0.1) == 124
    assert attempt.identity["termination"] == "EXCLUSIVE_ASSIGNED_JOB_HANDLE_ONLY"


def test_remote_budget_hard_ceiling_does_not_reserve_next_attempt():
    from allday_asr.v3.adapters.codex.daily_bounded_attempt import (
        RemoteBudget,
        RemoteStartLimitReached,
    )

    budget = RemoteBudget(2)
    budget.reserve()
    budget.reserve()
    with pytest.raises(RemoteStartLimitReached):
        budget.reserve()
    assert budget.starts == 2


@pytest.mark.parametrize(
    "scope",
    [
        "previous_sources",
        "lookahead_sources",
        "open_context_sources",
        "major_sources",
        "task_sources",
    ],
)
def test_unsampled_context_revision_changes_fingerprint(scope):
    a = AnonymousAnalyzer()
    sources = {"current": rows(count=1), scope: rows(count=1, offset=100)}
    before = request_fingerprint(a, {"open_context": []}, sources, "p1", "s1")
    sources[scope][0]["revision"] += 1
    assert request_fingerprint(a, {"open_context": []}, sources, "p1", "s1") != before
