"""Synthetic relevance/security checks; no source, model or Calendar calls."""

import pytest
from tests.chat_fixture import record, NOW
from tests.test_chat_followups import env as env
from tests.test_chat_followups import candidate
from allday_asr.v3.domain.chat_followups import validate_candidate, actor_key
from allday_asr.v3.domain.chat_admission import evidence_digest
from allday_asr.v3.domain.chat_projection import (
    semantic_text,
    semantic_record,
    semantic_value,
)
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.ports.chat_data import ChatDataError


def review(env, r, decision="text_only", origin="human_denial", **changes):
    c = candidate(r, **changes)
    v = validate_candidate(c, [r], "dataset", env.links, NOW + 86400)
    binding = {k: v[k] for k in ("source_key", "dataset", "conversation_key")}
    binding["evidence_digest"] = evidence_digest(v)
    return env.service.review_relevance(
        c,
        [r],
        "dataset",
        env.links,
        binding=binding,
        decision=decision,
        origin=origin,
        review_ref="synthetic-feedback",
    )


@pytest.mark.parametrize(
    "text",
    [
        "我明天去买新手机",
        "@alice 明天见",
        "我决定换个头像",
        "他说明天会帮忙",
        "如果有空我来负责",
        "我答应不会处理",
        "他说我来负责",
    ],
)
def test_self_speech_or_at_is_not_a_task(env, text):
    r = record(801, text)
    assert (
        env.service.apply(candidate(r), [r], "dataset", env.links)["effect"]
        == "excluded"
    )
    assert env.service.list()["items"] == []
    with env.db.read() as db:
        assert (
            db.execute("SELECT count(*) FROM event_current_states").fetchone()[0] == 0
        )
        assert db.execute("SELECT count(*) FROM reminder_schedules").fetchone()[0] == 0


def test_person_relation_is_not_blanket_topic_permission(env):
    a = record(802, "我去参加课程", sender="bob")
    b = record(803, "我去买手机", sender="bob")
    env.links[actor_key(a, "bob")] = "known-friend"
    review(env, a, "cared", "human_selection")
    assert (
        env.service.apply(candidate(a), [a], "dataset", env.links)["effect"]
        == "applied"
    )
    assert (
        env.service.apply(candidate(b), [b], "dataset", env.links)["effect"]
        == "excluded"
    )
    row = env.service.list()["items"][0]
    assert row["category"] == "cared" and row["event_kind"] == "important_experience"


def test_unknown_relationship_conditional_interest_is_audit_only(env):
    r = record(804, "我明天去打疫苗", sender="bob")
    review(env, r, "conditional", "conditional_interest")
    env.links[actor_key(r, "bob")] = "friend"
    assert (
        env.service.apply(candidate(r), [r], "dataset", env.links)["effect"]
        == "excluded"
    )
    assert env.service.list()["items"] == []


def test_human_denial_is_persistent_issue_specific_and_not_manual_ignore(env):
    r = record(805, "我来负责这件事")
    other = record(806, "我来负责另外一件事")
    review(env, r)
    for _ in range(2):
        assert (
            env.service.apply(candidate(r), [r], "dataset", env.links)["effect"]
            == "excluded"
        )
    assert (
        env.service.apply(candidate(other), [other], "dataset", env.links)["effect"]
        == "applied"
    )
    with env.db.read() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM chat_followup_admissions WHERE origin='human_denial'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT sum(ignored) FROM chat_followup_sources").fetchone()[0]
            == 0
        )


def test_unknown_default_can_be_reinterpreted_after_explicit_self_mapping(env):
    r = record(807, "我来负责材料")
    assert env.service.apply(candidate(r), [r], "dataset", {})["effect"] == "excluded"
    assert (
        env.service.apply(candidate(r), [r], "dataset", env.links)["effect"]
        == "applied"
    )


def test_verified_reply_to_self_distinguishes_waiting(env):
    own = record(808, "请你负责寄材料")
    promise = record(809, "我给你寄材料", sender="bob")
    promise["reply"] = {"record_id": own["record_id"]}
    c = candidate(
        promise,
        recipient_account_ids=["alice"],
        evidence=[
            {"record_id": own["record_id"], "quote": own["text"]},
            {"record_id": promise["record_id"], "quote": promise["text"]},
        ],
    )
    assert (
        env.service.apply(c, [own, promise], "dataset", env.links)["effect"]
        == "applied"
    )
    assert env.service.list()["items"][0]["category"] == "waiting"


def test_review_withdraws_only_unmodified_automatic_event_and_replays_idempotently(env):
    r = record(810, "我来负责材料")
    result = env.service.apply(candidate(r), [r], "dataset", env.links)
    review(env, r)
    review(env, r)
    assert env.service.list()["items"] == []
    with env.factory().reading() as uow:
        state = uow.knowledge.get_event(result["event_id"])
        assert state.status.value == "cancelled" and state.revision == 2
        assert (
            uow.followups.source(state.payload["chat_followup"]["source_key"])[
                "ignored"
            ]
            == 0
        )
        assert len(uow.knowledge.event_history(state.event_id)) == 2


def test_manual_change_prevents_review_and_rule_withdrawal(env):
    r = record(811, "我来负责材料")
    env.service.apply(candidate(r), [r], "dataset", env.links)
    row = env.service.list()["items"][0]
    env.service.act(row["source_key"], "edit", {"title": "人工保留"}, row["revision"])
    assert review(env, r)["effect"] == "human_conflict"
    assert env.service.list()["items"][0]["payload"]["title"] == "人工保留"


def test_bad_binding_and_changed_evidence_cannot_target_live_state(env):
    r = record(812, "我来负责材料")
    env.service.apply(candidate(r), [r], "dataset", env.links)
    newer = {**r, "record_revision": 2}
    with pytest.raises(ChatDataError, match="RELEVANCE_EVIDENCE_CHANGED"):
        review(env, newer)
    v = validate_candidate(candidate(r), [r], "dataset", env.links, NOW)
    binding = {k: v[k] for k in ("source_key", "dataset", "conversation_key")}
    binding["evidence_digest"] = "wrong"
    with pytest.raises(ChatDataError, match="RELEVANCE_BINDING_CHANGED"):
        env.service.review_relevance(
            candidate(r),
            [r],
            "dataset",
            env.links,
            binding=binding,
            decision="text_only",
            origin="human_denial",
            review_ref="fixture",
        )
    assert env.service.list()["items"][0]["revision"] == 1


def test_nested_xml_projection_keeps_current_comment_quote_author_and_stable_id():
    inner = '<msg><img aeskey="SYNTHETIC_SECRET" cdnthumburl="https://cdn.example/media"/><appmsg><title>引用正文</title></appmsg></msg>'
    import html

    outer = (
        "<msg><appmsg><title><![CDATA[当前评论]]></title><refermsg><displayname>引用作者</displayname><fromusr>quoted-id</fromusr><createtime>123</createtime><content>"
        + html.escape(inner)
        + "</content></refermsg></appmsg></msg>"
    )
    r = record(813, outer)
    projected = semantic_record(r)
    text = projected["text"]
    assert all(
        x in text
        for x in [
            "当前评论",
            "引用正文",
            "引用作者",
            "quoted-id",
            "[引用开始]",
            "[引用结束]",
        ]
    )
    assert projected["record_id"] == r["record_id"]
    assert not any(
        x in packed(projected)
        for x in ["SYNTHETIC_SECRET", "aeskey", "cdnthumburl", "https://cdn"]
    )
    assert r["text"] == outer


@pytest.mark.parametrize(
    "text",
    [
        '<msg><img aeskey="SYNTHETIC_SECRET">',
        '<!DOCTYPE msg [<!ENTITY e SYSTEM "file:///etc/passwd">]><msg><title>&e;</title></msg>',
        '<msg><video aeskey="SYNTHETIC_SECRET"/><appmsg><title>视频评论</title></appmsg></msg>',
        "<msg><appmsg><title>小程序标题</title><weappinfo><aeskey>SYNTHETIC_SECRET</aeskey><cdnurl>https://cdn.example</cdnurl></weappinfo></appmsg></msg>",
    ],
)
def test_media_or_unparsed_fragment_never_falls_back_to_raw(text):
    safe = semantic_text(text)
    assert not any(
        x in safe for x in ["SYNTHETIC_SECRET", "aeskey", "file:///", "https://cdn"]
    )
    assert safe


def test_structured_reference_keys_and_forward_cdata_are_sanitized():
    value = semantic_value(
        {
            "quote": {
                "author": "speaker",
                "aeskey": "SYNTHETIC_SECRET",
                "content": "<msg><recorditem><![CDATA[<msg><appmsg><title>转发原话</title></appmsg></msg>]]></recorditem></msg>",
            }
        }
    )
    assert "转发原话" in packed(value) and "speaker" in packed(value)
    assert "SYNTHETIC_SECRET" not in packed(value)


def test_legacy_xml_raw_fingerprint_survives_repeated_withdrawal(env):
    import json
    from tests.test_chat_followups import selected

    r = record(
        814,
        "<msg><appmsg><title>承诺资料</title><refermsg><content>旧引用</content></refermsg><aeskey>SYNTHETIC_SECRET</aeskey></appmsg></msg>",
    )
    selected(env.service, candidate(r), [r], env.links)
    env.service.apply(candidate(r), [r], "dataset", env.links)
    row = env.service.list()["items"][0]
    # Seed only the old delivery representation in the isolated fixture.
    with env.db.transaction() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload_json FROM event_current_states WHERE event_id=?",
                (row["event_id"],),
            ).fetchone()[0]
        )
        evidence = payload["chat_followup"]["evidence"][0]
        evidence["text"] = r["text"]
        evidence.pop("raw_text_sha256")
        db.execute(
            "UPDATE event_current_states SET payload_json=? WHERE event_id=?",
            (packed(payload), row["event_id"]),
        )
    assert review(env, r)["effect"] == "excluded"
    assert review(env, r)["effect"] == "excluded"
    with env.factory().reading() as uow:
        state = uow.knowledge.get_event(row["event_id"])
        assert state.revision == 2 and "SYNTHETIC_SECRET" not in packed(state.payload)


def test_review_export_uses_same_projection_without_original_cache_mutation(tmp_path):
    from tools.export_chat_review_projection import export
    import zipfile

    source = tmp_path / "source"
    source.mkdir()
    r = record(
        815,
        "<msg><appmsg><title>当前标题</title><weappinfo><aeskey>SYNTHETIC_SECRET</aeskey></weappinfo></appmsg></msg>",
    )
    files = {
        "source_records.json": {"records": [r]},
        "candidates.json": {"candidates": []},
        "coverage.json": {"complete": False},
        "source_calibration.json": {},
        "input_metrics.json": {},
    }
    for name, value in files.items():
        (source / name).write_text(packed(value), encoding="utf8")
    archive = export(source, tmp_path / "out")
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        content = "\n".join(z.read(n).decode("utf8") for n in z.namelist())
        assert "SYNTHETIC_SECRET" not in content and "当前标题" in content
    assert "SYNTHETIC_SECRET" in (source / "source_records.json").read_text(
        encoding="utf8"
    )


def test_rule_exclusion_from_changed_or_old_source_cannot_withdraw_newer_event(env):
    r = record(816, "我来负责材料")
    env.service.apply(candidate(r), [r], "dataset", env.links)
    changed = {**r, "record_revision": 2}
    # Lost identity plus a changed revision must not silently replace current proof.
    result = env.service.apply(candidate(changed), [changed], "dataset", {})
    assert result["effect"] == "evidence_conflict"
    assert env.service.list()["items"][0]["revision"] == 1


def test_comments_around_media_and_unparsed_suffix_are_preserved():
    safe = semantic_text('当前评论<msg><img aeskey="SYNTHETIC_SECRET"/></msg>后续评论')
    assert "当前评论" in safe and "后续评论" in safe and "SYNTHETIC_SECRET" not in safe
    malformed = semantic_text('已有评论<msg><img aeskey="SYNTHETIC_SECRET">')
    assert (
        "已有评论" in malformed
        and "未解析" in malformed
        and "SYNTHETIC_SECRET" not in malformed
    )
