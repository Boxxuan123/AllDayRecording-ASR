"""Summary-only publication from existing Daily events; never invokes event analysis."""
from allday_asr.v3.domain.hashing import canonical_json_sha256
from .daily_event_persistence import event_resource
from .daily_structured_summary import persist_summary, select_major
from .daily_overview import prepare_overview
from .insight_projections import _date, _period
from .generation_context import current_generation, publication_guard


def recompute_summary(service, summary_date, timezone_name):
    context = current_generation.get()
    if context is None:
        raise ValueError("summary recompute requires an explicit bounded execution")
    day = _date(summary_date).isoformat()
    start, end = _period(_date(summary_date), timezone_name)
    key = f"scoped-generation:{context.execution_id}:daily-summary:{day}:{timezone_name}"

    def sources(uow):
        states = [uow.knowledge.get_event(v["event_id"])
                  for v in uow.insights.daily_event_states(day, timezone_name)]
        states = [s for s in states if s.derivation_status == "active" and s.status.value == "active"]
        utterances = tuple(sorted({r["utterance_id"] for s in states for r in s.payload["evidence_snapshots"]}))
        tasks = uow.insights.daily_tasks(utterances)
        snapshot = {"events": [event_resource(s) for s in states], "tasks": tasks}
        return states, tasks, canonical_json_sha256(snapshot)

    with service._uow_factory().reading() as uow:
        prior = uow.idempotency.response(key)
        if prior is not None:
            return prior
        states, tasks, digest = sources(uow)
    if not states:
        raise ValueError("summary recompute requires existing active Daily events")
    synthesis, error = prepare_overview(service, select_major([event_resource(s) for s in states]),
                                        tasks, day, allow_model=True)
    if error:
        raise RuntimeError("summary overview did not complete")
    publication_guard()
    with service._uow_factory() as uow:
        prior = uow.idempotency.response(key)
        if prior is not None:
            return prior
        current, current_tasks, current_digest = sources(uow)
        if current_digest != digest:
            raise ValueError("summary event sources changed before publication")
        if not uow.idempotency.begin(key, "scoped-daily-summary"):
            raise ValueError("summary publication is already owned")
        result = persist_summary(uow, current, current_tasks, day, timezone_name,
                                 start, end, service._now(), synthesis=synthesis)
        uow.idempotency.complete(key, result)
        return result
