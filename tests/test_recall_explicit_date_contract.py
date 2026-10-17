"""Generation and strict date validation agree on source-written calendar years."""
from copy import deepcopy
import json
import pytest
from allday_asr.v3.adapters.codex.recall_schema import evidence_schema
from allday_asr.v3.application.recall_investigation import _claims
from allday_asr.v3.domain.recall_investigation import SCHEMA, _validate
from allday_asr.v3.domain.recall_event_time import full_explicit_dates

def time_schema(rows):
    return evidence_schema(SCHEMA, rows)["properties"]["claims"]["items"]["properties"]["event_time"]

def event(quote, basis="explicit", first="2026-09-21", last="2026-09-24", ref="E1"):
    return dict(basis=basis, first=first, last=last, evidence_id=ref, quote=quote)

def test_partial_year_range_generation_rejects_explicit_and_preserves_inference():
    quote="设备年检预受理时间为2026年9月21日至9月24日，每日08:00-16:00。"
    rows=[dict(id="E1",quote=quote)]
    schema=time_schema(rows)
    bad=event(quote)
    with pytest.raises(ValueError): _validate(bad,schema)
    good={**bad,"basis":"inferred"}
    _validate(good,schema)
    evidence={"E1":dict(quote=quote,sent_at=1789869600)}
    claim=dict(kind="statement",text="根据通知上下文推定年检受理为2026-09-21至2026-09-24；结束日年份是推定。",citations=["E1"],event_time=good)
    assert _claims({"claims":[claim]},evidence,{"timezone":"Asia/Singapore"})==[claim]
    with pytest.raises(ValueError,match="full explicit date"):
        _claims({"claims":[{**claim,"event_time":bad}]},evidence,{"timezone":"Asia/Singapore"})

@pytest.mark.parametrize("quote",["2026-9-21至2026-9-24", "2026/09/21至2026/09/24", "2026年09月21日至2026年9月24日"])
def test_full_dates_accept_calendar_spelling_without_inventing_year(quote):
    rows=[dict(id="E1",quote=quote)]
    value=event(quote)
    _validate(value,time_schema(rows))
    claim=dict(kind="statement",text="收到设备检查时间通知。",citations=["E1"],event_time=value)
    assert _claims({"claims":[claim]},{"E1":rows[0]},{"timezone":"Asia/Singapore"})

def test_other_records_full_date_cannot_license_explicit_for_ellipsis():
    quote="9月21日至9月24日可以检查。"
    schema=time_schema([dict(id="E1",quote=quote),dict(id="E2",quote="2026年9月21日至2026年9月24日")])
    with pytest.raises(ValueError): _validate(event(quote),schema)
    _validate(event(quote,"inferred"),schema)

def test_invalid_calendar_dates_and_metadata_are_not_explicit_text():
    assert full_explicit_dates("2026-02-30，2026/13/01，2026年2月29日")==[]
    schema=time_schema([dict(id="E1",quote="9月21日",sent_at=1789948800)])
    with pytest.raises(ValueError): _validate(event("9月21日",last="2026-09-21"),schema)

def test_explicit_schema_stays_bounded_and_does_not_mutate_static_contract():
    old=deepcopy(SCHEMA)
    rows=[dict(id=f"E{i+1}",quote=" ".join(f"{2020+i}-01-{d:02d}" for d in range(1,29))) for i in range(48)]
    schema=time_schema(rows)
    branches=schema["anyOf"][2:]
    assert len(json.dumps(branches,ensure_ascii=False).encode())<=3500
    assert SCHEMA==old
    with pytest.raises(ValueError): _validate(event("",ref="E999"),schema)

def test_unknown_cannot_hide_a_dated_claim():
    claim=dict(kind="statement",text="检查将在2026-09-21完成。",citations=["E1"],event_time=dict(basis="unknown",first="",last="",evidence_id="",quote=""))
    with pytest.raises(ValueError,match="dated assertion"):
        _claims({"claims":[claim]},{"E1":dict(quote="还没确认日期")},{"timezone":"Asia/Singapore"})
