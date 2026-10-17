"""No model calls: stream completion, privacy, and compact final protocol."""

import json
from types import SimpleNamespace
import pytest
from openai_codex.generated.v2_all import (
    ItemCompletedNotification,
    TurnCompletedNotification,
)
from allday_asr.v3.adapters.codex.stream_diagnostics import StreamDiagnostics
from allday_asr.v3.domain.recall_investigation import (
    SCHEMA,
    bounded_final_protocol,
    checked,
)
from tests.test_agentic_recall import response, claim
from tests.test_model_execution_runner import call, successful_result
from allday_asr.v3.adapters.codex.model_execution_runner import ModelExecutionRunner


def item(text):
    return ItemCompletedNotification.model_validate(
        {
            "completedAtMs": 1,
            "threadId": "thread",
            "turnId": "turn",
            "item": {"type": "agentMessage", "id": "item", "text": text},
        }
    )


def completed(status="completed"):
    return TurnCompletedNotification.model_validate(
        {
            "threadId": "thread",
            "turn": {"id": "turn", "status": status, "items": [], "durationMs": 8},
        }
    )


def test_stream_records_metadata_but_not_sensitive_content(tmp_path):
    path = tmp_path / "stream.json"
    secret = '{"private":"sensitive synthetic phrase"}'

    def stream():
        yield SimpleNamespace(
            method="item/agentMessage/delta", payload=SimpleNamespace(delta=secret)
        )
        yield SimpleNamespace(method="item/completed", payload=item(secret))
        yield SimpleNamespace(method="turn/completed", payload=completed())

    observer = StreamDiagnostics(path)
    result = observer.consume(SimpleNamespace(id="turn", stream=stream))
    data = json.loads(path.read_text("utf-8"))
    assert result.final_response == secret
    assert data["turn_completed"] and data["partial_json_valid"]
    assert data["agent_bytes"] == len(secret.encode())
    assert "sensitive synthetic phrase" not in path.read_text("utf-8")
    assert data["server_duration_ms"] == 8


def test_partial_json_and_stream_error_never_become_success(tmp_path):
    path = tmp_path / "stream.json"

    def stream():
        yield SimpleNamespace(
            method="item/agentMessage/delta",
            payload=SimpleNamespace(delta='{"private":'),
        )
        raise TimeoutError("injected")

    with pytest.raises(TimeoutError):
        StreamDiagnostics(path).consume(SimpleNamespace(id="turn", stream=stream))
    data = json.loads(path.read_text("utf-8"))
    assert not data["turn_completed"] and not data["partial_json_valid"]
    assert data["error_type"] == "TimeoutError"
    assert data["events"]["item/agentMessage/delta"]["count"] == 1


def test_complete_message_without_completed_turn_is_rejected(tmp_path):
    def stream():
        yield SimpleNamespace(method="item/completed", payload=item("{}"))

    with pytest.raises(RuntimeError, match="completed event"):
        StreamDiagnostics(tmp_path / "stream.json").consume(
            SimpleNamespace(id="turn", stream=stream)
        )


def test_base_instructions_are_scoped_and_part_of_frozen_identity(
    tmp_path, monkeypatch
):
    runner = ModelExecutionRunner(tmp_path / "receipts")
    seen = []

    def owned(identity, *a):
        seen.append(identity)
        return successful_result()

    monkeypatch.setattr(runner, "_owned_call", owned)
    call(runner)
    call(runner, task="history-investigation-v1")
    assert "base_instructions" not in seen[0]
    assert "historical-evidence" in seen[1]["base_instructions"]
    snapshots = [
        json.loads(p.read_text("utf-8"))["request"]
        for p in runner.receipt_dir.glob("*.request.snapshot.json")
    ]
    assert any("base_instructions" in x for x in snapshots)


def test_compact_final_keeps_citations_unknown_and_strict_time():
    schema, _ = bounded_final_protocol()
    value = response(
        "answer",
        claims=[claim("先前计划被后续更正，仍待确认。", ["a", "b", "c", "d"])],
        unknown=["来源仅有发言，实际执行尚未核实。"],
    )
    assert checked(value, schema)["claims"][0]["citations"] == ["a", "b", "c", "d"]
    assert SCHEMA["properties"]["claims"]["maxItems"] == 12
    value["claims"][0]["event_time"]["quote"] = "不能在未知时间附引文"
    with pytest.raises(ValueError):
        checked(value, schema)


def test_compact_final_rejects_overlong_prose_but_original_protocol_unchanged():
    schema, _ = bounded_final_protocol()
    value = response("answer", claims=[claim("甲" * 121, ["a"])])
    assert checked(value)
    with pytest.raises(ValueError):
        checked(value, schema)


def test_source_two_day_period_does_not_relax_query_scope():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from allday_asr.v3.domain.recall_event_time import source_relative_period
    from allday_asr.v3.domain.recall_query import dates

    zone = ZoneInfo("Asia/Singapore")
    now = int(datetime(2026, 10, 15, 12, tzinfo=zone).timestamp())
    period = source_relative_period(
        "前天没有出发，昨天也没有成行", "Asia/Singapore", now
    )
    assert [datetime.fromtimestamp(t, zone).date().isoformat() for t in period] == [
        "2026-10-13",
        "2026-10-14",
    ]
    with pytest.raises(ValueError):
        dates("前天和昨天", "Asia/Singapore", now)
    for quote in [
        "大前天和昨天",
        "前天或昨天",
        "前天昨天2026-10-15",
        "前天昨天明天",
        "昨晚和昨天",
    ]:
        with pytest.raises(ValueError):
            source_relative_period(quote, "Asia/Singapore", now)


def test_compact_wire_expands_only_fixed_fields():
    from allday_asr.v3.domain.recall_investigation import checked_final

    value = {
        "claims": [claim("尚待核实", ["a"])],
        "candidates": [],
        "unknown": ["不足"],
    }
    result = checked_final(value)
    assert result["mode"] == "answer" and result["actions"] == []
    assert result["claims"] == value["claims"]
    with pytest.raises(ValueError):
        checked_final({**value, "actions": []})


def test_adapter_adds_local_message_time_without_mutating_source(tmp_path, monkeypatch):
    from copy import deepcopy
    from threading import Event
    from allday_asr.v3.adapters.codex.recall_investigator import (
        RecallInvestigatorAdapter,
    )

    wire = {"claims": [claim("待核实", ["E1"])], "candidates": [], "unknown": []}
    seen = {}

    def run(self, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(final_response=json.dumps(wire))

    monkeypatch.setattr(ModelExecutionRunner, "run", run)
    payload = {
        "scope": {"timezone": "Asia/Singapore", "sent_from": 0, "sent_to": 100},
        "records": [{"id": "E1", "sent_at": 0, "revision": 1, "quote": "无关虚构原文"}],
        "round": 2,
        "query_id": "q",
        "model_budget_seconds": 25,
    }
    before = deepcopy(payload)
    result = RecallInvestigatorAdapter(tmp_path)(payload, Event())
    assert payload == before
    assert (
        json.loads(seen["prompt"])["records"][0]["sent_at_local"]
        == "1970-01-01T07:30:00+07:30"
    )
    assert set(seen["schema"]["properties"]) == {"claims", "candidates", "unknown"}
    assert result["actions"] == [] and seen["effort"] == "low"


def test_catalog_snapshot_freshness_identity_and_custom_config(tmp_path):
    from allday_asr.v3.adapters.codex.model_catalog_snapshot import snapshot_catalog

    home = tmp_path / "home"
    home.mkdir()
    cache = {
        "fetched_at": "2026-10-15T00:00:00Z",
        "client_version": "different-runtime",
        "models": [{"slug": "chosen"}],
    }
    source = home / "models_cache.json"
    source.write_text(json.dumps(cache), "utf-8")
    original = source.read_bytes()
    from datetime import datetime, timezone

    now = datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp()
    result = snapshot_catalog(tmp_path / "out", "chosen", home=home, now=now + 30)
    from pathlib import Path

    assert json.loads(Path(result["path"]).read_text("utf-8")) == {
        "models": cache["models"]
    }
    assert source.read_bytes() == original
    assert (
        snapshot_catalog(tmp_path / "out", "chosen", home=home, now=now + 3601) is None
    )
    assert (
        snapshot_catalog(tmp_path / "out", "missing", home=home, now=now + 30) is None
    )
    (home / "config.toml").write_text('model_catalog_json="custom.json"', "utf-8")
    assert snapshot_catalog(tmp_path / "out", "chosen", home=home, now=now + 30) is None


def test_mcp_overrides_disable_only_names_without_copying_config(tmp_path):
    from allday_asr.v3.adapters.codex.model_catalog_snapshot import (
        configured_mcp_overrides,
    )

    cfg = tmp_path / "config.toml"
    text = '[mcp_servers."custom_name"]\ncommand="secret-location"\nenabled=true\n'
    cfg.write_text(text, "utf-8")
    assert configured_mcp_overrides(home=tmp_path) == [
        'mcp_servers.custom_name.enabled=false'
    ]
    assert cfg.read_text("utf-8") == text


def test_existing_stream_receipt_access_denied_does_not_restart_stream(tmp_path, monkeypatch):
    from pathlib import Path
    from allday_asr.v3.adapters.codex import model_execution_runner as runner

    path = tmp_path / "stream.json"
    secret = '{"private":"synthetic local-only stream"}'
    original_replace = Path.replace
    consumed = []
    denied = []

    def replace(part, target):
        if Path(target) == path and path.exists() and len(denied) < 2:
            denied.append(part)
            exc = PermissionError("injected existing receipt lock")
            exc.winerror = 5
            raise exc
        return original_replace(part, target)

    def stream():
        consumed.append("started")
        yield SimpleNamespace(
            method="item/agentMessage/delta", payload=SimpleNamespace(delta=secret)
        )
        yield SimpleNamespace(method="item/completed", payload=item(secret))
        yield SimpleNamespace(method="turn/completed", payload=completed())

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    result = StreamDiagnostics(path).consume(SimpleNamespace(id="turn", stream=stream))
    data = json.loads(path.read_text("utf-8"))
    assert consumed == ["started"] and len(denied) == 2
    assert result.final_response == secret and data["turn_completed"]
    assert data["events"]["turn/completed"]["count"] == 1
    assert "synthetic local-only stream" not in path.read_text("utf-8")
    assert not list(tmp_path.glob("*.part"))
