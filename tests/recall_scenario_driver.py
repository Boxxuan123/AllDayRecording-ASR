"""Synthetic full product entry and exact outbound request capture; never a semantic oracle."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import time
import uuid
from tests.chat_fixture import FixtureMac, record
from allday_asr.v3.adapters.sqlite.database import V3Database
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.bootstrap.history_recall import compose_history_recall
from allday_asr.v3.adapters.codex.recall_investigator import RecallInvestigatorAdapter
from allday_asr.v3.adapters.codex.recall_model import RecallModelAdapter
from allday_asr.v3.adapters.codex.recall_planner import RecallPlannerAdapter
from allday_asr.v3.adapters.codex.model_execution_runner import ModelExecutionRunner

NOW = 1792110900
DAY = 1791993600  # 2026-10-15 00:00 +08


def contact(platform, room, label, peer=None, kind="c2c"):
    return dict(
        platform=platform,
        source_account_id="source-" + platform,
        id=room,
        conversation_type=kind,
        display_name=label,
        peer_account_id=peer,
    )


def message(
    i,
    text,
    sender,
    room,
    platform="qq",
    hour=13,
    minute=0,
    kind="group",
    day=DAY,
    label=None,
):
    r = record(i, text, platform, room, day + hour * 3600 + minute * 60, sender)
    r.update(
        conversation_type=kind,
        is_from_self=sender == "self-account",
        self_identity_basis="synthetic native direction",
        sender_display_name=label or sender,
        conversation_display_name=room,
    )
    return r


class ScenarioSource(FixtureMac):
    def __init__(self, scenario):
        super().__init__(scenario["records"])
        self.contacts = deepcopy(scenario["contacts"])
        self.scenario = scenario
        self.dataset = scenario.get("dataset", self.dataset)

    def request(self, route, body=None, **kw):
        if route != "/v1/conversations":
            value = super().request(route, body, **kw)
            if self.scenario.get("reverse_response_rows") and "items" in value:
                value["items"].reverse()
            return value
        self.calls.append((route, deepcopy(body), deepcopy(kw)))
        if self.failure:
            from allday_asr.v3.ports.chat_data import ChatDataError

            raise ChatDataError(self.failure)
        values = [
            c
            for c in self.contacts
            if all(c.get(k) == v for k, v in kw.get("scope", {}).items())
        ]
        start = int(kw.get("cursor") or 0)
        limit = min(kw.get("limit", 500), self.scenario.get("directory_page_size", 500))
        items = values[start : start + limit]
        return dict(
            items=items,
            has_more=start + len(items) < len(values),
            next_cursor=str(start + len(items)),
        )


def run_scenario(root, scenario, *, live=False, model_hook=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = V3Database.open(root / "core.sqlite3")
    cache = ChatCache(root / "chat/cache.sqlite3")
    source = ScenarioSource(scenario)
    cache.activate(source.dataset)
    if scenario.get("cached_directory"):
        from allday_asr.v3.adapters.sqlite.recall_directory import entry

        items = [entry(c, source.dataset) for c in scenario["contacts"]]
        (root / "chat/recall-directory.json").write_text(
            json.dumps(
                dict(
                    dataset=source.dataset,
                    items=items,
                    pages={
                        p: dict(complete=True, updated_at=time.time())
                        for p in ["qq", "wechat"]
                    },
                )
            ),
            "utf8",
        )
    cached = set(scenario.get("cached_record_ids", []))
    if cached:
        cache.receive(
            source.dataset, [r for r in source.records if r["record_id"] in cached]
        )
        cache.process(400)
    inputs = []
    wire = []
    outputs = []
    stages = {}
    from allday_asr.v3.application.recall_evidence_flow import EvidenceFlow

    original_record = EvidenceFlow.record

    def stage_record(self, stage, row, reason, relations=()):
        stages.setdefault(stage, set()).add(row.get("record_id", row.get("id")))
        return original_record(self, stage, row, reason, relations)

    def no_sdk(self, **kw):
        identity = self.prepare_request(
            **{
                k: kw[k]
                for k in ["task", "prompt", "instructions", "schema", "model", "effort"]
            }
        )
        wire.append(identity)
        value = dict(claims=[], unknown=["PROTOCOL_PROBE_ONLY_NOT_SEMANTIC_SUCCESS"])
        if "candidates" in kw["schema"]["properties"]:
            value["candidates"] = []
        if "mode" in kw["schema"]["properties"]:
            value.update(
                mode="answer",
                time=dict(kind="unchanged", first="", last=""),
                actions=[],
            )
            payload = json.loads(kw["prompt"])
            refs = [
                o
                for o in payload.get("objects", [])
                if o.get("relation_basis") and o.get("relation_evidence")
            ]
            if scenario.get("explore_related") and payload.get("round") == 1 and refs:
                value.update(
                    mode="search",
                    unknown=[],
                    actions=[
                        dict(
                            tool="search_messages",
                            purpose="Generic protocol exploration of observed rooms",
                            targets=[o["ref"] for o in refs[:2]],
                            terms=[],
                            window="episode",
                            evidence_ids=list(
                                dict.fromkeys(
                                    e for o in refs[:2] for e in o["relation_evidence"]
                                )
                            )[:6],
                        )
                    ],
                )
        if scenario.get("protocol_citation_probe") and value.get("mode") == "answer":
            rows = json.loads(kw["prompt"]).get("records", [])
            if rows:
                value["claims"] = [
                    dict(
                        kind="statement",
                        text="Protocol reference check only; not semantic grading.",
                        citations=[
                            "E999999"
                            if scenario.get("invalid_probe_citation")
                            else rows[0]["id"]
                        ],
                        event_time=dict(
                            basis="unknown", first="", last="", evidence_id="", quote=""
                        ),
                    )
                ]
        if "contact_keys" in kw["schema"]["properties"]:
            value = dict(
                intent="quote",
                relationship="topic",
                person_mention="",
                contact_keys=[],
                topics=[],
                time=dict(kind="unresolved", first="", last=""),
                clarification="PROTOCOL_PROBE_ONLY_NOT_SEMANTIC_SUCCESS",
            )
        return SimpleNamespace(final_response=json.dumps(value))

    original_run = ModelExecutionRunner.run

    def live_sdk(self, **kw):
        wire.append(
            self.prepare_request(
                **{
                    k: kw[k]
                    for k in [
                        "task",
                        "prompt",
                        "instructions",
                        "schema",
                        "model",
                        "effort",
                    ]
                }
            )
        )
        return original_run(self, **kw)

    def wrap(adapter):
        def call(payload, cancel):
            inputs.append(deepcopy(payload))
            if model_hook:
                model_hook(payload)
            if live:
                with patch.object(ModelExecutionRunner, "run", live_sdk):
                    value = adapter(payload, cancel)
            else:
                with patch.object(ModelExecutionRunner, "run", no_sdk):
                    value = adapter(payload, cancel)
            outputs.append(value)
            return value

        return call

    service = compose_history_recall(
        db,
        root,
        source,
        wrap(RecallModelAdapter(root)),
        investigator=wrap(RecallInvestigatorAdapter(root)),
        planner=wrap(RecallPlannerAdapter(root)),
    )
    request = dict(
        action="query",
        query_id="scenario_" + uuid.uuid4().hex,
        question=scenario["question"],
        timezone="Asia/Singapore",
    )
    request.update(scenario.get("filters", {}))
    import allday_asr.v3.application.history_recall as history

    started = time.monotonic()
    with (
        patch.object(
            history, "time", SimpleNamespace(time=lambda: NOW, monotonic=time.monotonic)
        ),
        patch.object(EvidenceFlow, "record", stage_record),
    ):
        result = service.execute("synthetic-owner", request)
    revoked = {}
    if scenario.get("revoke_after_first"):
        source.failure = "AUTHORIZATION_REVOKED"
        before = len(inputs)
        for operation, req in [
            ("query", {**request, "query_id": "revoke_" + uuid.uuid4().hex}),
            ("page", dict(action="page", query_id=request["query_id"])),
        ]:
            try:
                revoked[operation] = service.execute("synthetic-owner", req)
            except Exception as exc:
                revoked[operation] = dict(
                    error=getattr(exc, "code", type(exc).__name__),
                    status=getattr(exc, "status", None),
                )
        restarted = compose_history_recall(
            db,
            root,
            source,
            wrap(RecallModelAdapter(root)),
            investigator=wrap(RecallInvestigatorAdapter(root)),
            planner=wrap(RecallPlannerAdapter(root)),
        )
        try:
            revoked["cached_after_restart"] = restarted.execute(
                "synthetic-owner",
                {
                    **request,
                    "query_id": "cached_revoke_" + uuid.uuid4().hex,
                    "expand": False,
                },
            )
        except Exception as exc:
            revoked["cached_after_restart"] = dict(
                error=getattr(exc, "code", type(exc).__name__),
                status=getattr(exc, "status", None),
            )
        revoked["extra_model_calls"] = len(inputs) - before
    saved = deepcopy(service.results[("synthetic-owner", request["query_id"])][1])
    saved.get("_plan", {}).pop("_directory", None)
    return dict(
        request=request,
        result=result,
        internal=saved,
        inputs=inputs,
        wire=wire,
        outputs=outputs,
        revoked=revoked,
        stage_ids={k: sorted(v) for k, v in stages.items()},
        source_queries=source.calls,
        elapsed_ms=round((time.monotonic() - started) * 1000),
        semantic_verified=False,
    )


def selected_ids(run):
    handles = run["internal"].get("_citation_handles", {})
    selected = set()
    for request in run["wire"]:
        for row in json.loads(request["prompt"]).get("records", []):
            full = handles[row["id"]][0]
            selected.add(full.removeprefix("chat:"))
    return selected
