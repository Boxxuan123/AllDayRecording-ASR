"""Uses the existing deny-all/read-only Codex runtime for bounded inference."""

import json
import copy
from openai_codex import ApprovalMode, Sandbox
from openai_codex.types import ReasoningEffort
from allday_asr.v3.ports.daily_semantics import (
    DailySemanticResult,
    DailyReconciliationResult,
    DailyOverviewResult,
)
from .event_generator import CodexSemanticEventGenerator, _usage
from .daily_semantic_schema import DAILY_SEMANTIC_SCHEMA, INSTRUCTIONS, validate_output
from .daily_reconcile_schema import RECONCILE_SCHEMA, RECONCILE_INSTRUCTIONS
from .daily_reconcile_schema import FINAL_NORMALIZATION_INSTRUCTIONS
from .daily_reconcile_schema import MATERIALIZATION_REVIEW_INSTRUCTIONS
from .daily_reconcile_schema import NORMALIZATION_SCOPE_INSTRUCTIONS
from .daily_overview_schema import OVERVIEW_SCHEMA, OVERVIEW_INSTRUCTIONS


def _grounded_goal_schema(request):
    """Constrain IDs at generation time, rather than guessing a typo afterward."""
    schema = copy.deepcopy(RECONCILE_SCHEMA)
    candidates = request.get("candidates", [])
    context = request.get("open_context", [])
    properties = schema["properties"]["groups"]["items"]["properties"]
    count = max(1, len(candidates))
    schema["properties"]["groups"]["maxItems"] = count
    schema["properties"]["groups"]["description"] = (
        "Only groups containing CURRENT candidates. Open context is read-only continuation metadata, not standalone output groups."
    )
    properties["candidate_indices"]["items"]["maximum"] = count - 1
    source_roles = properties["topic_purity"]["properties"]["source_roles"]
    source_roles["maxItems"] = count
    source_roles["description"] = (
        "Exactly the indices in THIS group's candidate_indices, once each. No open-context roles and no other groups' candidates. Context source roles are preserved locally."
    )
    source_roles["items"]["properties"]["candidate_index"]["maximum"] = count - 1
    keys = [c["event_key"] for c in context] + [
        f"new:{i}" for i in range(max(1, len(candidates)))
    ]
    properties["event_key"]["enum"] = sorted(set(keys))
    refs = sorted(
        {r["utterance_id"] for c in candidates + context for r in c.get("evidence", [])}
    )
    if refs:
        for field in [
            "title_evidence_utterance_ids",
            "importance_evidence_utterance_ids",
        ]:
            properties[field]["items"]["enum"] = refs
        purity = properties["topic_purity"]["properties"]
        purity["materialization_evidence_utterance_ids"]["items"]["enum"] = refs
        purity["source_roles"]["items"]["properties"]["evidence_utterance_ids"][
            "items"
        ]["enum"] = refs
    return schema


def _grounded_overview_schema(request):
    schema = copy.deepcopy(OVERVIEW_SCHEMA)
    ids = sorted({e["event_id"] for e in request.get("major_events", [])})
    if ids:
        schema["properties"]["headline_source_event_ids"]["items"]["enum"] = ids
        schema["properties"]["overview_sentences"]["items"]["properties"][
            "source_event_ids"
        ]["items"]["enum"] = ids
    return schema


class CodexDailySemanticAnalyzer(CodexSemanticEventGenerator):
    provider = "codex"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompt_version = "daily-semantic-v1.1-prompt.1"
        self.extractor_version = "daily-semantic-v1.1-schema.1"
        self.reconcile_prompt_version = "daily-semantic-v1.2-reconcile-prompt.1"
        self.normalization_prompt_version = "daily-semantic-v1.2-normalize-prompt.1"
        self.normalization_review_prompt_version = (
            "daily-semantic-v1.2-value-review-prompt.4"
        )
        self.normalization_scope_prompt_version = (
            "daily-semantic-v1.2-scope-normalize-prompt.2"
        )
        self.reconcile_schema_version = "daily-semantic-v1.2-reconcile-schema.3"
        self.overview_prompt_version = "daily-semantic-v1.2-overview-prompt.1"
        self.overview_schema_version = "daily-semantic-v1.2-overview-schema.2"

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
            _grounded_goal_schema(request),
            self.reconcile_prompt_version,
            self.reconcile_schema_version,
        )
        return DailyReconciliationResult(tuple(payload["groups"]), provenance)

    def normalize(self, request: dict) -> DailyReconciliationResult:
        reviewing = request.get("version") == self.normalization_review_prompt_version
        scoped = (
            reviewing
            or request.get("version") == self.normalization_scope_prompt_version
        )
        payload, provenance = self._run_bounded(
            request,
            RECONCILE_INSTRUCTIONS
            + "\n"
            + FINAL_NORMALIZATION_INSTRUCTIONS
            + ("\n" + MATERIALIZATION_REVIEW_INSTRUCTIONS if reviewing else "")
            + ("\n" + NORMALIZATION_SCOPE_INSTRUCTIONS if scoped else ""),
            _grounded_goal_schema(request),
            request["version"] if scoped else self.normalization_prompt_version,
            self.reconcile_schema_version,
        )
        return DailyReconciliationResult(tuple(payload["groups"]), provenance)

    def overview(self, request: dict) -> DailyOverviewResult:
        payload, provenance = self._run_bounded(
            request,
            OVERVIEW_INSTRUCTIONS,
            _grounded_overview_schema(request),
            self.overview_prompt_version,
            self.overview_schema_version,
        )
        return DailyOverviewResult(payload, provenance)

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
