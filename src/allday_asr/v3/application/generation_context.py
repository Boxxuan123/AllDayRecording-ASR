"""Explicit generation budgets and receipts, independent of release/Git identity."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
import time


@dataclass
class GenerationContext:
    execution_id: str
    deadline: float
    cancelled: Event = field(default_factory=Event)
    receipts: list[dict] = field(default_factory=list)

    def check(self):
        if self.cancelled.is_set():
            raise InterruptedError("generation cancelled")
        if time.time() >= self.deadline:
            raise TimeoutError("generation deadline exceeded")


current_generation: ContextVar[GenerationContext | None] = ContextVar("generation", default=None)


@contextmanager
def generation_context(context):
    token = current_generation.set(context)
    try:
        context.check()
        yield context
    finally:
        current_generation.reset(token)


def publication_guard():
    context = current_generation.get()
    if context is not None:
        context.check()


def generation_provenance(*, inputs=None, model=None, rules=None, output_schema=1):
    from allday_asr.build_info import runtime_build
    from allday_asr.v3.domain.hashing import canonical_json_sha256
    context = current_generation.get()
    value = {"version": 1, "build": dict(runtime_build()), "model": model,
             "output_schema_version": output_schema}
    if inputs is not None:
        value.update(inputs=inputs, inputs_sha256=canonical_json_sha256(inputs))
    if rules is not None:
        value.update(rules=rules, rules_sha256=canonical_json_sha256(rules))
    if context is not None:
        value.update(execution_id=context.execution_id, model_receipts=list(context.receipts))
    return value
