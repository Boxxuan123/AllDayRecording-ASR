"""Anonymous SQLite publication fixtures; no analyzer, SDK or model invocation."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from allday_asr.v3.adapters.sqlite.daily_generation_queue import DailyGenerationQueue
from allday_asr.v3.adapters.sqlite.insight_repository import SqliteInsightRepository
from allday_asr.v3.application.daily_automation import DailyGenerationCoordinator
from allday_asr.v3.adapters.sqlite.daily_inventory import DailyHistoryInventory
from allday_asr.v3.domain.daily_semantics import SEMANTIC_VERSION
from allday_asr.v3.domain.hashing import canonical_json_sha256
from tests.test_v32_three_layer_knowledge import (
    V32ThreeLayerKnowledgeTests,
    UTTERANCE_ID,
    _event_submission,
)


@pytest.fixture
def publication():
    seed = V32ThreeLayerKnowledgeTests()
    seed.setUp()
    try:
        receipt = seed.knowledge.submit_generation(_event_submission())
        eid = seed.knowledge.accept_proposal(
            receipt["proposals"][0]["proposal_id"], "fixture"
        ).resource_id
        def now():
            return seed.now + timedelta(days=2)

        inventory = DailyHistoryInventory(seed.database, "fixture-version", now=now)
        day = inventory.scan()[0]["date"]
        with seed.database.transaction() as c:
            c.execute(
                "UPDATE event_current_states SET payload_json=? WHERE event_id=?",
                (
                    json.dumps(
                        {
                            "daily_event_version": SEMANTIC_VERSION,
                            "local_date": day,
                            "timezone": "Asia/Singapore",
                        }
                    ),
                    eid,
                ),
            )
        with seed.database.read() as c:
            gid = c.execute(
                "SELECT generation_id FROM generation_records LIMIT 1"
            ).fetchone()[0]
        state = SimpleNamespace(
            seed=seed,
            inventory=inventory,
            now=now,
            day=day,
            eid=eid,
            gid=gid,
            revision=0,
        )
        state.queue = DailyGenerationQueue(seed.database, now)
        yield state
    finally:
        seed.tearDown()


def publish(
    p,
    *,
    count=1,
    major=None,
    provenance=None,
    version=SEMANTIC_VERSION,
    status="complete",
    omit_major=False,
):
    p.revision += 1
    objective = {
        "generation_version": version,
        "semantic_status": status,
        "statistics": {"event_count": count},
        "source_event_revisions": {p.eid: 1} if count else {},
        "major_events": [] if major is None else major,
    }
    if omit_major:
        objective.pop("major_events")
    with p.seed.database.transaction() as c:
        SqliteInsightRepository(c).add_daily(
            summary_id="fixture-summary",
            revision=p.revision,
            summary_date=p.day,
            timezone="Asia/Singapore",
            period_start=p.seed.now.isoformat(),
            period_end=(p.seed.now + timedelta(days=1)).isoformat(),
            objective=objective,
            narrative={},
            input_sha256=canonical_json_sha256(objective),
            generation_id=p.gid,
            provenance={
                "overview_synthesis": provenance
                if provenance is not None
                else {"provider": "local", "remote": False}
            },
            status="active",
            created_by="fixture",
            created_at=p.now().isoformat(),
        )


def scan(p):
    return next(r for r in p.inventory.scan() if r["date"] == p.day)


def job(p, *, status="SUCCEEDED", fingerprint=None, version=None):
    item = scan(p)
    item.update(classification="CURRENT_COMPLETE", needs_backfill=False)
    if fingerprint is not None:
        item["source_fingerprint"] = fingerprint
    if version is not None:
        item["generation_version"] = version
    jid = p.queue.observe([item])[0]
    if status != "SUCCEEDED":
        with p.seed.database.transaction() as c:
            c.execute(
                "UPDATE daily_generation_jobs SET status=?,attempts=1,automatic_retry_blocked=1 WHERE job_id=?",
                (status, jid),
            )
    return jid


def test_secondary_finals_with_explicit_local_empty_major_are_current(publication):
    p = publication
    publish(p)
    job(p)
    result = scan(p)
    assert result["classification"] == "CURRENT_COMPLETE"
    assert not result["needs_backfill"]


@pytest.mark.parametrize(
    "major,provenance",
    [
        ([{"event_ids": ["fixture-event"]}], {"provider": "local", "remote": False}),
        ({}, {"provider": "local", "remote": False}),
        ("", {"provider": "local", "remote": False}),
        ([], {"provider": "local", "remote": True}),
        ([], {"provider": "local", "remote": 0}),
        ([], {"provider": "local", "remote": "false"}),
        ([], {"provider": "fixture", "remote": False}),
        ([], {}),
    ],
)
def test_nonapproved_overview_not_treated_as_complete(publication, major, provenance):
    publish(publication, major=major, provenance=provenance)
    assert scan(publication)["classification"] == "LEGACY_ONLY"


def test_missing_major_metadata_not_treated_as_empty_major(publication):
    p = publication
    publish(p, omit_major=True)
    assert scan(p)["classification"] == "LEGACY_ONLY"


@pytest.mark.parametrize(
    "model,prompt,expected",
    [
        ("gpt-5.6-luna", "daily-semantic-v1.2-overview-prompt.1", "CURRENT_COMPLETE"),
        ("old-model", "daily-semantic-v1.2-overview-prompt.1", "LEGACY_ONLY"),
        ("gpt-5.6-luna", "old-prompt", "LEGACY_ONLY"),
    ],
)
def test_major_overview_keeps_approved_model_prompt_guard(
    publication, model, prompt, expected
):
    publish(
        publication,
        major=[{"event_ids": [publication.eid]}],
        provenance={"model": model, "prompt_version": prompt},
    )
    assert scan(publication)["classification"] == expected


@pytest.mark.parametrize(
    "fingerprint,version",
    [
        ("old-source", None),
        (None, "old-generation"),
        ("old-source", "old-generation"),
    ],
)
def test_old_failure_identity_does_not_poison_current(
    publication, fingerprint, version
):
    p = publication
    publish(p)
    job(p, status="FAILED_TERMINAL", fingerprint=fingerprint, version=version)
    job(p)
    assert scan(p)["classification"] == "CURRENT_COMPLETE"


def test_latest_complete_empty_takes_precedence_over_legacy(publication):
    p = publication
    publish(p, version="daily-local-v1")
    publish(p, count=0)
    with p.seed.database.transaction() as c:
        c.execute("UPDATE event_current_states SET status='cancelled'")
    assert scan(p)["classification"] == "CURRENT_COMPLETE_EMPTY"


@pytest.mark.parametrize(
    "version,status,expected",
    [
        ("daily-local-v1", "complete", "LEGACY_ONLY"),
        (SEMANTIC_VERSION, "pending", "LEGACY_ONLY"),
        (SEMANTIC_VERSION, "failed", "LEGACY_ONLY"),
    ],
)
def test_summary_row_alone_is_not_completion(publication, version, status, expected):
    publish(publication, version=version, status=status)
    assert scan(publication)["classification"] == expected


def test_current_source_fingerprint_mismatch_is_stale(publication):
    p = publication
    publish(p)
    job(p)
    with p.seed.database.transaction() as c:
        c.execute(
            "UPDATE utterances SET revision=revision+1,updated_at=?",
            ((p.now() + timedelta(seconds=1)).isoformat(),),
        )
    assert scan(p)["classification"] == "STALE_SOURCE"


@pytest.mark.parametrize(
    "change,expected",
    [
        ("UPDATE utterances SET status='stale'", "NO_SOURCE"),
        ("UPDATE utterances SET text='   '", "NO_SOURCE"),
        (
            'UPDATE utterances SET evidence_json=\'{"sound_source":"media"}\'',
            "NO_SOURCE",
        ),
        (
            "UPDATE recording_sessions SET status_code='processing'",
            "UPSTREAM_INCOMPLETE",
        ),
        ("UPDATE processing_runs SET status='running'", "UPSTREAM_INCOMPLETE"),
    ],
)
def test_invalid_or_incomplete_source_not_current(publication, change, expected):
    publish(publication)
    with publication.seed.database.transaction() as c:
        c.execute(change)
    assert scan(publication)["classification"] == expected


def test_event_revision_mismatch_stays_stale(publication):
    p = publication
    publish(p)
    job(p)
    with p.seed.database.transaction() as c:
        c.execute("UPDATE event_current_states SET revision=revision+1")
    assert scan(p)["classification"] == "STALE_SOURCE"


def test_current_terminal_identity_remains_held(publication):
    p = publication
    publish(p)
    job(p, status="FAILED_TERMINAL")
    before = p.queue.states()
    result = scan(p)
    assert (
        result["classification"] == "FAILED_TERMINAL" and not result["needs_backfill"]
    )
    p.queue.observe([result])
    assert p.queue.claim([result]) is None
    assert p.queue.states() == before


@pytest.mark.parametrize(
    "trigger", ["startup", "receiver_sync", "asr_complete", "completed_day_check"]
)
def test_corrected_inventory_no_duplicate_or_model_start(publication, trigger):
    p = publication
    publish(p)
    job(p)

    def forbidden(_):
        raise AssertionError("No service/model may start for current publication")

    coordinator = DailyGenerationCoordinator(
        p.seed.database, forbidden, "fixture-version", now=p.now
    )
    before = p.queue.states()
    assert not coordinator.catch_up(trigger)["created_job_ids"]
    assert coordinator.run_once() is None
    assert p.queue.states() == before


@pytest.mark.parametrize("stale", [False, True])
def test_missing_and_stale_are_still_discovered(publication, stale):
    p = publication
    if stale:
        publish(p)
        job(p)
        with p.seed.database.transaction() as c:
            c.execute(
                "UPDATE utterances SET revision=revision+1,updated_at=?",
                ((p.now() + timedelta(seconds=1)).isoformat(),),
            )
    result = scan(p)
    assert result["classification"] == ("STALE_SOURCE" if stale else "MISSING")
    assert result["needs_backfill"]
    p.queue.observe([result])
    claimed = p.queue.claim([result])
    assert (
        claimed is not None
        and claimed["source_fingerprint"] == result["source_fingerprint"]
    )


def test_anonymous_local_midnight_boundary_uses_utterance_local_date(publication):
    p = publication
    result = scan(p)
    assert result["active_utterance_count"] == 1
    with p.seed.database.read() as c:
        stamp = c.execute(
            "SELECT start_at FROM utterances WHERE utterance_id=?", (UTTERANCE_ID,)
        ).fetchone()[0]
    assert stamp[:10] != result["date"]  # UTC 16:00 belongs to the next local day.
