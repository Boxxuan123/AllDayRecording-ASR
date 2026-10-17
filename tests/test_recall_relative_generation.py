"""Source wording constrains date basis at generation and consumption."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from allday_asr.v3.domain.recall_investigation import bounded_final_protocol,checked
from allday_asr.v3.adapters.codex.recall_schema import evidence_schema
from allday_asr.v3.application.recall_investigation import _claims
from tests.test_agentic_recall import claim
ZONE="Asia/Singapore"
SENT=int(datetime(2025,6,27,13,tzinfo=ZoneInfo(ZONE)).timestamp())


def value(quote,basis="relative",day="2025-06-18",last="2025-06-20",ref="E1"):
    c=claim("原文提及的日期需按消息月份推断，实际安排仍未证实。",[ref])
    c['event_time']=dict(basis=basis,first=day,last=last,evidence_id=ref,quote=quote)
    return dict(mode="answer",time=dict(kind="unchanged",first="",last=""),actions=[],claims=[c],candidates=[],unknown=[])

@pytest.mark.parametrize("quote",["18号到20号出差", "6月18日到6月20日出差", "月底到下月初可能出差"])
def test_no_indexical_day_cannot_be_generated_or_consumed_as_relative(quote):
    row=dict(id="E1",quote=quote,sent_at=SENT)
    schema,_=bounded_final_protocol();schema=evidence_schema(schema,[row])
    v=value(quote);before=deepcopy(v)
    with pytest.raises(ValueError):checked(v,schema)
    with pytest.raises(ValueError):_claims(v,{"E1":row},{"timezone":ZONE})
    assert v==before


def test_contextual_month_dates_stay_inferred_with_support_not_unknown():
    quote="18号到20号可能出差";row=dict(id="E1",quote=quote,sent_at=SENT)
    schema,_=bounded_final_protocol();schema=evidence_schema(schema,[row])
    v=value(quote,"inferred")
    assert checked(v,schema)['claims']==v['claims']
    assert _claims(v,{"E1":row},{"timezone":ZONE})==v['claims']
    unknown=deepcopy(v);unknown['claims'][0]['text']='2025-06-18出发已经确定';unknown['claims'][0]['event_time']=dict(basis='unknown',first='',last='',evidence_id='',quote='')
    with pytest.raises(ValueError):_claims(unknown,{"E1":row},{"timezone":ZONE})


def test_relative_generation_requires_wording_on_the_cited_record():
    rows=[dict(id="E1",quote="18号到20号可能出差",sent_at=SENT),dict(id="E2",quote="明晚再确认，尚未成行",sent_at=SENT)]
    schema,_=bounded_final_protocol();schema=evidence_schema(schema,rows)
    with pytest.raises(ValueError):checked(value('18号到20号可能出差'),schema)
    valid=value('明晚',day='2025-06-28',last='2025-06-28',ref='E2')
    assert checked(valid,schema) and _claims(valid,{r['id']:r for r in rows},{'timezone':ZONE})
    wrong=deepcopy(valid);wrong['claims'][0]['event_time']['evidence_id']='E1';wrong['claims'][0]['citations']=['E1']
    with pytest.raises(ValueError):checked(wrong,schema)
