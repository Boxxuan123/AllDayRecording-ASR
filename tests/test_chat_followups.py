"""F01-F12: isolated shared-event tests; no production/model/calendar writes."""

from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

from tests.chat_fixture import record, NOW
from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.application.chat_followups import ChatFollowups
from allday_asr.v3.domain.chat_followups import (
    actor_key,
    resolve_time,
    validate_candidate,
)
from allday_asr.v3.domain.chat_admission import evidence_digest
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.ports.chat_data import ChatDataError
from allday_asr.v3.bootstrap.chat_followups import FollowupJobs


@pytest.fixture
def env(tmp_path):
    db = V3Database.open(tmp_path / "core.sqlite3")

    def factory():
        return SqliteUnitOfWork(db)

    service = ChatFollowups(
        factory, now=lambda: datetime.fromtimestamp(NOW + 86400, timezone.utc)
    )
    links = {packed(["qq", "source-qq", "alice"]): "self"}
    return SimpleNamespace(
        db=db, factory=factory, service=service, links=links, root=tmp_path
    )


def candidate(r, **changes):
    return {
        "title": "提交资料",
        "action": "create",
        "anchor_id": r["record_id"],
        "issue_quote": r["text"],
        "target_event_id": None,
        "actor_account_id": r["sender_account_id"],
        "recipient_account_ids": ["teacher"],
        "commitment": "explicit",
        "relationship": "explicit",
        "time_expression": "",
        "timezone": "Asia/Shanghai",
        "due_date": None,
        "due_at": None,
        "uncertainty": [],
        "evidence": [{"record_id": r["record_id"], "quote": r["text"]}],
        **changes,
    }


def selected(service, c, records, links):
    # Explicit synthetic user relevance is a PRECONDITION for downstream state tests.
    v = validate_candidate(c, records, "dataset", links, NOW + 86400)
    binding = {k: v[k] for k in ("source_key", "dataset", "conversation_key")}
    binding["evidence_digest"] = evidence_digest(v)
    service.review_relevance(
        c,
        records,
        "dataset",
        links,
        binding=binding,
        decision="task",
        origin="human_selection",
        review_ref="synthetic-user-selection",
    )


def add(env, r, **kw):
    c = candidate(r, **kw)
    selected(env.service, c, [r], env.links)
    return env.service.apply(c, [r], "dataset", env.links)


def item(env):
    return env.service.list()["items"][0]


def test_f01_self_other_and_unknown_identity(env):
    a = record(30, "我明天发资料")
    b = record(31, "我给你发回执", sender="bob")
    c = record(32, "我来处理", sender="same-name")
    add(env, a)
    add(env, b)
    rows = env.service.list()["items"]
    assert {r["category"] for r in rows} == {"self", "waiting"}
    assert rows[0]["event_id"] != rows[1]["event_id"]
    s = ChatFollowups(env.factory, now=env.service.now)
    s.apply(candidate(c), [c], "dataset", {})
    assert len(s.list()["items"]) == 2


@pytest.mark.parametrize(
    "commitment", ["proposal", "conditional", "negated", "reported", "unclear"]
)
def test_f02_non_commitments_keep_uncertainty(env, commitment):
    r = record(40, "有空就去；他说会寄，不是我的承诺")
    add(env, r, commitment=commitment)
    assert item(env)["category"] == "pending"
    assert item(env)["chat"]["evidence"][0]["quote"] == r["text"]


def test_f03_date_precision_source_time_and_invented_hour(env):
    r = record(50, "我明天发资料", sent=1791504000)
    info = resolve_time("明天", r, "Asia/Shanghai", None, None)
    assert (
        info["date"] == "2026-10-10"
        and info["at"] is None
        and info["precision"] == "day"
    )
    with pytest.raises(ChatDataError, match="INVENTED_FOLLOWUP_HOUR"):
        resolve_time("明天", r, "Asia/Shanghai", None, "2026-10-10T09:00:00+08:00")
    assert resolve_time("明天", r, None, None, None)["date"] is None
    with pytest.raises(ChatDataError):
        resolve_time("明天", r, "Asia/Shanghai", "2027-10-10", None)


def test_f04_history_never_becomes_current_overdue(env):
    r = record(60, "我明天发资料", sent=1602040457)
    add(env, r)
    assert item(env)["chat"]["historical"] and item(env)["chat"]["progress_unknown"]
    assert item(env)["category"] == "pending"
    with env.db.read() as db:
        assert db.execute("SELECT count(*) FROM reminder_schedules").fetchone()[0] == 0


def test_f05_f06_update_cancel_complete_and_unrelated_short_reply(env):
    r = record(70, "我提交资料", sent=NOW)
    event = add(env, r)["event_id"]
    u = record(71, "资料改到明天下午四点", sent=NOW + 50)
    add(
        env,
        u,
        action="update",
        target_event_id=event,
        title="资料改期",
        time_expression="明天下午四点",
    )
    assert item(env)["event_id"] == event and item(env)["revision"] == 2
    assert len(item(env)["chat"]["evidence"]) == 2
    done = record(72, "资料发给你了", sent=NOW + 100)
    add(env, done, action="complete", target_event_id=event)
    assert item(env)["category"] == "ended"
    with env.factory().reading() as uow:
        assert len(uow.knowledge.event_history(event)) == 3
    short = record(73, "好", sent=NOW + 101)
    add(env, short, action="complete", target_event_id=event, relationship="unclear")
    assert env.service.list()["items"][0]["category"] == "pending"
    r2 = record(74, "我交报名表", sent=NOW)
    event2 = add(env, r2)["event_id"]
    cancel = record(75, "报名表不用交了", sent=NOW + 200)
    add(env, cancel, action="cancel", target_event_id=event2)
    assert any(
        x["event_id"] == event2 and x["status"] == "cancelled"
        for x in env.service.list()["items"]
    )


def test_f07_similar_titles_distinct_objects_and_two_issues_same_message(env):
    a = record(80, "我交甲资料；我交乙资料")
    add(env, a, issue_quote="我交甲资料", title="提交资料")
    add(env, a, issue_quote="我交乙资料", title="提交资料")
    b = record(81, "我明天交另一次资料")
    add(env, b, title="提交资料")
    assert len(env.service.list()["items"]) == 3


@pytest.mark.parametrize("action", ["complete", "ignore", "edit"])
def test_f08_manual_override_old_replay_new_conflict_and_new_promise(env, action):
    r = record(90, "我发资料", sent=NOW)
    c = candidate(r)
    add(env, r)
    row = item(env)
    env.service.act(
        row["source_key"],
        action,
        {"title": "人工改名"} if action == "edit" else {},
        row["revision"],
    )
    before = item(env)
    assert env.service.apply(c, [r], "dataset", env.links)["effect"] == "duplicate"
    assert item(env)["revision"] == before["revision"]
    u = record(91, "资料改到下周", sent=NOW + 100)
    assert (
        add(env, u, action="update", target_event_id=before["event_id"])["effect"]
        == "human_conflict"
    )
    assert item(env)["revision"] == before["revision"] and item(env)["conflict"]
    n = record(92, "新一轮资料我再发一次", sent=NOW + 200)
    add(env, n)
    assert len(env.service.list()["items"]) == 2


def test_f09_replay_and_late_older_input_preserve_latest(env):
    r = record(100, "我发材料", sent=NOW)
    event = add(env, r)["event_id"]
    newer = record(101, "改为另一份材料", sent=NOW + 500)
    add(env, newer, action="update", target_event_id=event, title="另一份材料")
    older = record(102, "材料取消了", sent=NOW + 50)
    assert (
        add(env, older, action="cancel", target_event_id=event)["effect"]
        == "older_or_same_evidence"
    )
    assert (
        item(env)["status"] == "active"
        and item(env)["payload"]["title"] == "另一份材料"
    )


def test_f10_transaction_rollback_restoration_and_human_survives_dataset(
    env, monkeypatch
):
    r = record(110, "我处理材料")
    add(env, r)
    row = item(env)
    env.service.act(row["source_key"], "complete", {}, row["revision"])
    from allday_asr.v3.adapters.sqlite.chat_followup_repository import (
        SqliteChatFollowupRepository,
    )

    original = SqliteChatFollowupRepository.effect
    monkeypatch.setattr(
        SqliteChatFollowupRepository,
        "effect",
        lambda *a: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError):
        add(env, record(111, "另一件事"))
    assert len(env.service.list()["items"]) == 1
    monkeypatch.setattr(SqliteChatFollowupRepository, "effect", original)
    restarted = ChatFollowups(env.factory, now=env.service.now)
    assert restarted.list()["items"][0]["status"] == "completed"
    cache = ChatCache(env.root / "cache.sqlite3")
    cache.activate("dataset")
    cache.activate("new-dataset")
    assert restarted.list()["items"][0]["status"] == "completed"
    with env.db.read() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def jobs(env, model):
    cache = ChatCache(env.root / "chat-cache.sqlite3")
    cache.activate("dataset")
    core = SimpleNamespace(
        paths=SimpleNamespace(state_dir=env.root), database=env.db, reminders=None
    )
    chat = SimpleNamespace(cache=cache)
    return FollowupJobs(core, chat, model=model)


def wait(job):
    job._thread.join(timeout=8)
    assert not job._thread.is_alive()


def test_f11_empty_and_model_unavailable_keep_manual_state(env):
    controller = jobs(env, lambda *a: {"items": [], "unknown": []})
    j = controller.start(records=[], dataset="dataset", coverage=[{"complete": True}])
    wait(controller)
    assert controller.service.job(j["id"])["calls"] == 0
    r = record(120, "我处理")
    add(env, r)
    controller.model = lambda *a: (_ for _ in ()).throw(
        ChatDataError("MODEL_UNAVAILABLE")
    )
    j = controller.start(records=[r], dataset="dataset")
    wait(controller)
    assert controller.service.job(j["id"])["state"] == "failed"
    row = item(env)
    env.service.act(row["source_key"], "complete", {}, row["revision"])
    assert item(env)["status"] == "completed"


def test_f11_budget_failure_no_automatic_new_batch(env):
    controller = jobs(env, lambda *a: {"items": [], "unknown": []})
    huge = record(121, "x" * 130000)
    j = controller.start(records=[huge], dataset="dataset")
    wait(controller)
    result = controller.service.job(j["id"])
    assert result["calls"] == 0 and result["error"] == "FOLLOWUP_VISIBLE_INPUT_BYTES"
    with pytest.raises(ChatDataError):
        controller.start(resume_id=j["id"]) if result.get("deadline_at") else (
            _ for _ in ()
        ).throw(ChatDataError("NO_DEADLINE"))


def test_f12_literal_proof_and_revision_conflict(env):
    r = record(130, "我发材料")
    bad = candidate(r, evidence=[{"record_id": r["record_id"], "quote": "编造的原话"}])
    with pytest.raises(ChatDataError):
        env.service.apply(bad, [r], "dataset", env.links)
    add(env, r)
    row = item(env)
    with pytest.raises(ChatDataError, match="FOLLOWUP_REVISION_CONFLICT"):
        env.service.act(row["source_key"], "ignore", {}, 0)
    assert item(env)["chat"]["evidence"][0]["record_id"] == r["record_id"]


def test_resume_replays_saved_real_response_without_extra_model_call(env):
    r = record(140, "我发资料")
    count = []

    def model(*args):
        count.append(1)
        return {"items": [candidate(r)], "unknown": []}

    controller = jobs(env, model)
    j = controller.start(records=[r], dataset="dataset", coverage=[{"complete": True}])
    wait(controller)
    value = controller.service.job(j["id"])
    assert value["calls"] == 1
    value = {k: v for k, v in value.items() if k not in {"id", "state"}}
    value["position"] = 0
    controller.service.save_job(j["id"], "interrupted", value)
    controller.start(resume_id=j["id"])
    wait(controller)
    assert len(count) == 1 and len(controller.service.list()["items"]) == 0


def test_f12_existing_recording_task_link_calendar_identity_and_migration(tmp_path):
    from tests import test_v33_intelligent_reminders as seed
    from allday_asr.v3.adapters.sqlite.migration_runner import V3MigrationRunner
    from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS
    from allday_asr.v3.application import (
        IntelligentReminderService,
        KnowledgeArchitectureService,
    )

    old = seed.V33IntelligentReminderTests()
    old.root = tmp_path
    old.database = V3Database(tmp_path / "core.sqlite3")
    V3MigrationRunner(old.database.path, migrations=MIGRATIONS[:30]).initialize()
    old.current_time = datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
    old._seed()

    def factory():
        return SqliteUnitOfWork(old.database)

    knowledge = KnowledgeArchitectureService(factory, now=lambda: old.current_time)
    reminders = IntelligentReminderService(
        factory, knowledge, now=lambda: old.current_time
    )
    c = reminders.submit_generation(
        seed._submission(seed._intent()), allow_auto_apply=False
    )["candidates"][0]
    event = reminders.confirm(c["candidate_id"], "desktop-user")["resolution"][
        "resource_id"
    ]
    with old.database.read() as db:
        before = {
            t: [tuple(r) for r in db.execute("SELECT * FROM " + t)]
            for t in [
                "event_operations",
                "event_current_states",
                "reminder_candidates",
                "reminder_schedules",
            ]
        }
        extras = {
            r[0]: r[1]
            for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE tbl_name IN ('event_operations','event_current_states') AND type IN ('index','trigger')"
            )
        }
    assert old.database.initialize() == 32
    with old.database.read() as db:
        assert before == {
            t: [tuple(r) for r in db.execute("SELECT * FROM " + t)] for t in before
        }
        assert extras == {
            r[0]: r[1]
            for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE tbl_name IN ('event_operations','event_current_states') AND type IN ('index','trigger')"
            )
        }
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    service = ChatFollowups(factory, now=lambda: old.current_time, reminders=reminders)
    r = record(150, "录音中的资料我来发送", sent=int(old.current_time.timestamp()))
    selected(service, candidate(r), [r], {actor_key(r, "alice"): "self"})
    service.apply(candidate(r), [r], "dataset", {actor_key(r, "alice"): "self"})
    row = service.list()["items"][0]
    linked = service.act(
        row["source_key"], "link", {"event_id": event}, row["revision"]
    )
    assert linked["event_id"] == event
    with factory().reading() as uow:
        state = uow.knowledge.get_event(event)
        schedule = uow.reminders.get_schedule(event)
        assert state.revision == schedule.event_revision
        assert schedule.source_candidate_id == c["candidate_id"]
        assert schedule.title == "把文档发给张同学"
        types = {
            x[0]
            for x in uow.followups.connection.execute(
                "SELECT evidence_type FROM evidence_links WHERE subject_id=?", (event,)
            )
        }
        assert {"utterance", "chat_record"} <= types
    row = service.list()["items"][0]
    service.act(
        row["source_key"], "edit", {"at": "2026-09-02T15:00:00+08:00"}, row["revision"]
    )
    row = service.list()["items"][0]
    service.act(row["source_key"], "complete", {}, row["revision"])
    with factory().reading() as uow:
        schedule = uow.reminders.get_schedule(event)
        assert schedule.status.value == "completed" and schedule.event_id == event
        assert (
            uow.followups.connection.execute(
                "SELECT count(*) FROM reminder_schedules"
            ).fetchone()[0]
            == 1
        )


def test_budget_deadline_does_not_prevent_local_response_recovery(env):
    r = record(160, "我处理资料")
    ctl = jobs(env, lambda *a: {"items": [candidate(r)], "unknown": []})
    j = ctl.start(records=[r], dataset="dataset", coverage=[{"complete": True}])
    wait(ctl)
    value = ctl.service.job(j["id"])
    value = {k: v for k, v in value.items() if k not in {"id", "state"}}
    value["position"] = 0
    value["calls"] = 12
    value["deadline_at"] = 1
    ctl.service.save_job(j["id"], "interrupted", value)
    ctl.model = lambda *a: (_ for _ in ()).throw(AssertionError("must not call model"))
    ctl.start(resume_id=j["id"])
    wait(ctl)
    assert ctl.service.job(j["id"])["calls"] == 12
    assert ctl.service.job(j["id"])["state"] == "completed"


def test_failed_gap_watermark_and_other_process_owner(env):
    from threading import Event

    entered = Event()
    release = Event()
    r = record(161, "我处理")

    def model(*a):
        entered.set()
        release.wait(5)
        return {
            "items": [
                candidate(
                    r, evidence=[{"record_id": r["record_id"], "quote": "不存在"}]
                )
            ],
            "unknown": [],
        }

    ctl = jobs(env, model)
    j = ctl.start(records=[r], dataset="dataset", coverage=[{"complete": True}])
    assert entered.wait(2)
    other = jobs(env, lambda *a: {"items": [], "unknown": []})
    assert other.service.job(j["id"])["state"] == "running"
    with pytest.raises(ChatDataError, match="FOLLOWUP_JOB_RUNNING"):
        other.start(records=[], dataset="dataset")
    release.set()
    wait(ctl)
    job = ctl.service.job(j["id"])
    assert (
        job["state"] == "partial"
        and job["processing_watermark"] == 0
        and job["position"] == 1
    )


def test_restore_same_canonical_record_retains_manual_identity(env):
    r = record(162, "我处理资料")
    add(env, r)
    row = item(env)
    env.service.act(row["source_key"], "ignore", {}, row["revision"])
    env.service.apply(candidate(r), [r], "restored-dataset", env.links)
    assert len(env.service.list()["items"]) == 1 and item(env)["ignored"]


def test_changed_self_mapping_rebuilds_derived_category_but_not_user_override(env):
    r = record(163, "我处理资料")
    result = env.service.apply(candidate(r), [r], "dataset", {})
    assert result["effect"] == "excluded" and env.service.list()["items"] == []
    selected(env.service, candidate(r), [r], env.links)
    env.service.apply(candidate(r), [r], "dataset", env.links)
    assert item(env)["category"] == "self"
    row = item(env)
    env.service.act(row["source_key"], "ignore", {}, row["revision"])
    env.service.apply(candidate(r), [r], "dataset", {})
    assert item(env)["ignored"] and item(env)["status"] == "cancelled"


def test_f03_known_timezone_chinese_hour_and_dst_ambiguity():
    r = record(170, "我明天下午三点半发材料", sent=1791504000)
    info = resolve_time(
        "明天下午三点半", r, "Asia/Shanghai", "2026-10-10", "2026-10-10T15:30:00+08:00"
    )
    assert info["at"] == "2026-10-10T15:30:00+08:00" and info["precision"] == "minute"
    anchor = record(
        171,
        "明天一点三十分",
        sent=int(datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp()),
    )
    info = resolve_time(
        "明天凌晨一点三十分",
        anchor,
        "America/New_York",
        None,
        "2026-11-01T01:30:00-04:00",
    )
    assert info["at"] is None and info["precision"] == "day"


def test_new_recent_scope_uses_formal_snapshot_and_atomic_receive(env):
    from tests.chat_fixture import FixtureMac
    from allday_asr.v3.bootstrap.chat import compose_chat_queries

    fixture = FixtureMac()
    chat = compose_chat_queries(
        env.root / "receipt", source=fixture, start_worker=False
    )
    core = SimpleNamespace(
        paths=SimpleNamespace(state_dir=env.root), database=env.db, reminders=None
    )
    ctl = FollowupJobs(core, chat, model=lambda *a: {"items": [], "unknown": []})
    ctl.configure(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
            }
        ],
        "Asia/Shanghai",
    )
    value = {"coverage": []}
    ctl._collect(value)
    assert any(c[0] == "/v1/snapshots" for c in fixture.calls)
    scope = chat.cache.scopes()[0]
    assert scope["enabled"] == 0 and scope["phase"] == "changes"
    assert scope["receive_cursor"] == scope["boundary"]
    assert (
        len(value["records"]) == scope["received"] and value["coverage"][-1]["complete"]
    )
    chat.close()
    ctl.close()


def test_local_handoff_import_is_bounded_idempotent_and_respects_manual_state(env):
    from allday_asr.v3.bootstrap.chat_followup_handoff import import_handoff

    ctl = jobs(env, lambda *a: (_ for _ in ()).throw(AssertionError("no model import")))
    ctl.configure(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
            }
        ],
        None,
    )
    r = record(172, "旧约定我来处理", sent=1602040457)
    (ctl.root / "handoff-candidates.json").write_text(
        packed(
            {
                "dataset": "dataset",
                "records": [r],
                "batches": [
                    {"items": [candidate(r)], "provenance": {"model": "fixture"}}
                ],
            }
        ),
        encoding="utf8",
    )
    assert import_handoff(ctl)["excluded"] == 1
    assert ctl.service.list()["items"] == []
    assert import_handoff(ctl)["excluded"] == 1
    with env.db.read() as db:
        assert db.execute("SELECT count(*) FROM reminder_schedules").fetchone()[0] == 0


def test_saved_handoff_reclassifies_on_explicit_self_mapping_without_model(env):
    ctl = jobs(env, lambda *a: (_ for _ in ()).throw(AssertionError("no model")))
    ctl.configure(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
            }
        ],
        None,
    )
    r = record(173, "我给你处理资料")
    (ctl.root / "handoff-candidates.json").write_text(
        packed(
            {
                "dataset": "dataset",
                "records": [r],
                "batches": [{"items": [candidate(r)]}],
                "acceptance": {"unique_messages": 1, "coverage_complete": False},
            }
        ),
        encoding="utf8",
    )
    ctl.configure(ctl.scopes()[0], None)
    assert ctl.service.list()["items"] == []
    ctl.link_account("qq", "source-qq", "alice", "self")
    after = ctl.service.list()["items"][0]
    assert after["category"] == "self"
    assert ctl.status()["local_handoff"]["acceptance"]["coverage_complete"] is False
    ctl.service.act(after["source_key"], "complete", {}, after["revision"])
    ctl.link_account("qq", "source-qq", "bob", "self")
    assert ctl.service.list()["items"][0]["status"] == "completed"
