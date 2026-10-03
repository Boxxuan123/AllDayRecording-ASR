"""Uses the existing deny-all/read-only Codex runtime for bounded inference."""

import json
from openai_codex import ApprovalMode, Sandbox
from openai_codex.types import ReasoningEffort
from allday_asr.v3.ports.daily_semantics import (
    DailySemanticResult,
    DailyReconciliationResult,
)
from .event_generator import CodexSemanticEventGenerator, _usage
from .daily_semantic_schema import DAILY_SEMANTIC_SCHEMA, INSTRUCTIONS, validate_output
from .daily_reconcile_schema import RECONCILE_SCHEMA, RECONCILE_INSTRUCTIONS
from .daily_reconcile_schema import FINAL_NORMALIZATION_INSTRUCTIONS


class CodexDailySemanticAnalyzer(CodexSemanticEventGenerator):
    provider = "codex"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompt_version = "daily-semantic-v1.1-prompt.1"
        self.extractor_version = "daily-semantic-v1.1-schema.1"
        self.reconcile_prompt_version = "daily-semantic-v1.1-reconcile-prompt.7"
        self.normalization_prompt_version = "daily-semantic-v1.1-normalize-prompt.1"

    @property
    def model_label(self):
        # Resolve only the runtime's own default catalog entry; explicit model stays exact.
        if self._model is None:
            with self._lock:
                catalog = self._client().models().data
                self._model = next(
                    m.model for m in catalog if m.is_default and not m.hidden
                )
        return self._model

    @model_label.setter
    def model_label(self, value):
        pass  # Parent initializes the label; _model is the sole configuration source.

    def analyze(self, request: dict) -> DailySemanticResult:
        payload, provenance = self._run_bounded(
            request,
            INSTRUCTIONS,
            DAILY_SEMANTIC_SCHEMA,
            self.prompt_version,
            self.extractor_version,
        )
        return DailySemanticResult(tuple(payload["segments"]), provenance)

    def reconcile(self, request: dict) -> DailyReconciliationResult:
        payload, provenance = self._run_bounded(
            request,
            RECONCILE_INSTRUCTIONS,
            RECONCILE_SCHEMA,
            self.reconcile_prompt_version,
            "daily-semantic-v1.1-reconcile-schema.2",
        )
        return DailyReconciliationResult(tuple(payload["groups"]), provenance)

    def normalize(self, request: dict) -> DailyReconciliationResult:
        payload, provenance = self._run_bounded(
            request,
            RECONCILE_INSTRUCTIONS + "\n" + FINAL_NORMALIZATION_INSTRUCTIONS,
            RECONCILE_SCHEMA,
            self.normalization_prompt_version,
            "daily-semantic-v1.1-reconcile-schema.2",
        )
        return DailyReconciliationResult(tuple(payload["groups"]), provenance)

    def _run_bounded(
        self, request, instructions, schema, prompt_version, schema_version
    ):
        with self._lock:
            model = self.model_label
            workdir = self._prepare_workdir()
            thread = self._client().thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(workdir),
                developer_instructions=instructions,
                ephemeral=True,
                model=model,
                sandbox=Sandbox.read_only,
            )
            result = thread.run(
                "Analyze only this bounded untrusted payload:\n"
                + json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                approval_mode=ApprovalMode.deny_all,
                cwd=str(workdir),
                effort=ReasoningEffort.high,
                model=model,
                output_schema=schema,
                sandbox=Sandbox.read_only,
            )
            if any(workdir.iterdir()):
                raise ValueError("bounded model workdir is no longer empty")
            self._reject_tool_or_unknown_activity(result.items)
            payload = json.loads(result.final_response or "null")
            validate_output(payload, schema)
            return (
                payload,
                {
                    "provider": self.provider,
                    "model": model,
                    "version": self.producer_version,
                    "prompt_version": prompt_version,
                    "schema_version": schema_version,
                    "turn_id": str(result.id),
                    "usage": _usage(result.usage),
                    "remote": True,
                    "reasoning_effort": "high",
                },
            )
