"""Read-only synthesis published as a revision of the existing stable daily summary."""

from copy import deepcopy
from datetime import datetime, timezone as utc
from threading import Lock
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import stable_ulid, new_ulid
from allday_asr.v3.domain.knowledge import KnowledgeLayer
from allday_asr.v3.domain.unified_review import admit_review, validate_review
from allday_asr.v3.application.daily_event_persistence import local_generation
from allday_asr.v3.application.insight_projections import _date, _period
import json
import os
from allday_asr.v3.application.file_lock import day_worker_lock


class UnifiedReviewService:
    def __init__(self, sources, uow_factory, model, root, now=None):
        self.sources, self.uow_factory, self.model, self.root = (
            sources,
            uow_factory,
            model,
            root,
        )
        self.now = now or (lambda: datetime.now(utc.utc))
        self.lock = Lock()

    def get(self, day, timezone):
        _period(_date(day), timezone)
        with self.uow_factory().reading() as uow:
            try:
                value = uow.insights.daily(stable_ulid("daily-summary", day, timezone))
            except KeyError:
                return {"date": day, "timezone": timezone, "status": "not_generated"}
            review = value["objective"].get(
                "unified_review"
            ) or uow.insights.last_unified_review(value["summary_id"])
        if review and review.get("status") == "ready":
            current_digest = canonical_json_sha256(self.sources.load(day, timezone))
            if current_digest != review.get("input_sha256"):
                return {**review, "status": "input_changed"}
        return review or {"date": day, "timezone": timezone, "status": "not_generated"}

    def refresh_saved(self):
        """Only dates explicitly generated before; no history discovery or inference."""
        if not self.root.exists():
            return
        with self.lock:
            for path in self.root.glob("*.input.json"):
                saved = json.loads(path.read_text(encoding="utf-8"))
                day, timezone = saved["date"], saved["timezone"]
                with day_worker_lock(
                    path.with_name(path.name.replace(".input.json", ".lock"))
                ) as acquired:
                    if not acquired:
                        continue
                    result = self.get(day, timezone)
                    with self.uow_factory().reading() as uow:
                        current = uow.insights.daily(
                            stable_ulid("daily-summary", day, timezone)
                        )
                    if result.get("status") == "input_changed" or not current[
                        "objective"
                    ].get("unified_review"):
                        self._publish(
                            day, timezone, self.sources.load(day, timezone), result
                        )

    def generate(self, day, timezone):
        _period(_date(day), timezone)
        key = canonical_json_sha256([day, timezone])
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock, day_worker_lock(self.root / (key + ".lock")) as acquired:
            if not acquired:
                return {**self.get(day, timezone), "status": "busy"}
            return self._generate(day, timezone, key)

    def revise(self, day, timezone, value, *, expected_input_sha256, reason):
        """Explicit evidence-based local correction; never invokes the model or tasks."""
        _period(_date(day), timezone)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("revision reason required")
        key = canonical_json_sha256([day, timezone])
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock, day_worker_lock(self.root / (key + ".lock")) as acquired:
            if not acquired:
                raise ValueError("review is busy")
            inputs = self.sources.load(day, timezone)
            digest = canonical_json_sha256(inputs)
            previous = self.get(day, timezone)
            if (
                digest != expected_input_sha256
                or previous.get("input_sha256") != digest
            ):
                raise ValueError(
                    "revision input changed; reload evidence before editing"
                )
            if not previous.get("generated_at"):
                raise ValueError("revision requires an existing generated review")
            grounded = validate_review(value, inputs)
            if previous.get("status") == "ready" and all(
                previous.get(k) == v for k, v in grounded.items()
            ):
                self._publish(day, timezone, inputs, previous)
                return {**previous, "reused": True}
            result = {
                **previous,
                **grounded,
                "status": "ready",
                "coverage": inputs["coverage"],
                "revised_at": self.now().isoformat(),
                "revision_reason": reason.strip(),
            }
            for field in (
                "reused",
                "validation_rejections",
                "error",
                "attempted_at",
                "attempted_input_sha256",
            ):
                result.pop(field, None)
            if canonical_json_sha256(self.sources.load(day, timezone)) != digest:
                raise ValueError("revision input changed; previous review retained")
            self._publish(day, timezone, inputs, result)
            _atomic_json(self.root / (key + ".output.json"), result)
            return result

    def _generate(self, day, timezone, key):
        inputs = self.sources.load(day, timezone)
        digest = canonical_json_sha256(inputs)
        previous = self.get(day, timezone)
        if previous.get("input_sha256") == digest and previous.get("status") == "ready":
            self._publish(day, timezone, inputs, previous)
            return {**previous, "reused": True}
        _atomic_json(self.root / (key + ".input.json"), inputs)
        try:
            if not inputs["evidence"]:
                raise ValueError("所选日期没有可用的现有证据")
            value, provenance = self.model(inputs)
            result = admit_review(value, inputs)
            # Provider inference cannot commit a result for superseded input.
            if canonical_json_sha256(self.sources.load(day, timezone)) != digest:
                raise ValueError("生成期间资料发生变化，已保留原回顾")
            result.update(
                date=day,
                timezone=timezone,
                status="ready",
                input_sha256=digest,
                generated_at=self.now().isoformat(),
                coverage=inputs["coverage"],
                version="unified-daily-review-v1",
                provenance=provenance,
            )
            _atomic_json(self.root / (key + ".output.json"), result)
        except Exception as exc:
            error = {
                "status": "failed",
                "attempted_at": self.now().isoformat(),
                "attempted_input_sha256": digest,
                "error": str(exc)[:400],
            }
            _atomic_json(self.root / (key + ".failure.json"), error)
            result = {**previous, **error, "date": day, "timezone": timezone}
            result.setdefault("coverage", inputs["coverage"])
            result.setdefault("claims", [])
            result.setdefault("evidence", [])
            result.setdefault(
                "limitations", ["本次生成未完成，上一份有效内容（若有）已保留。"]
            )
        self._publish(day, timezone, inputs, result)
        return result

    def _publish(self, day, timezone, inputs, result):
        now = self.now()
        sid = stable_ulid("daily-summary", day, timezone)
        start, end = _period(_date(day), timezone)
        with self.uow_factory() as uow:
            try:
                old = uow.insights.daily(sid)
            except KeyError:
                old = None
            # Preserve existing recording summary and every manual task decision.
            objective = deepcopy(old["objective"]) if old else _empty_objective(day)
            if objective.get("unified_review") == result:
                return
            objective["unified_review"] = result
            digest = canonical_json_sha256(objective)
            generation = local_generation(
                uow,
                KnowledgeLayer.MEMORY,
                digest,
                {
                    "rule_version": "unified-daily-review-v1",
                    "date": day,
                    "review_input_sha256": result.get("input_sha256"),
                    "coverage": inputs["coverage"],
                    "model_provenance": result.get("provenance"),
                },
                now,
            )
            revision = uow.insights.next_daily_revision(sid)
            uow.insights.add_daily(
                summary_id=sid,
                revision=revision,
                summary_date=day,
                timezone=timezone,
                period_start=start.isoformat(),
                period_end=end.isoformat(),
                objective=objective,
                narrative=old["narrative"] if old else {},
                input_sha256=digest,
                generation_id=generation.generation_id,
                provenance=old["provenance"] if old else {},
                status=old["status"] if old else "active",
                created_by="local:unified-review",
                created_at=now.isoformat(),
            )
            uow.insights.add_operation(
                new_ulid(),
                "daily_summary",
                sid,
                revision,
                "generate",
                "local:unified-review",
                {"input_sha256": digest},
                now.isoformat(),
            )
            uow.changes.append(
                "daily_summary",
                sid,
                revision,
                "upsert",
                {
                    "summary_id": sid,
                    "revision": revision,
                    "summary_date": day,
                    "timezone": timezone,
                    "status": "active",
                    "derivation_status": old["derivation_status"] if old else "active",
                    "created_at": now.isoformat(),
                    "objective": {k: v for k, v in objective.items() if k != "events"},
                    "provenance": old["provenance"] if old else {},
                },
            )


def _empty_objective(day):
    return {
        "date": day,
        "headline": "当天回顾",
        "headline_event_ids": [],
        "key_events": [],
        "tasks_created": [],
        "tasks_completed": [],
        "tasks_pending": [],
        "people_interacted": [],
        "unresolved_items": [],
        "source_event_ids": [],
        "source_event_revisions": {},
        "generation_version": "unified-daily-review-v1",
        "semantic_status": "pending",
        "statistics": {},
        "decisions": [],
        "new_todos": [],
        "unresolved": [],
    }


def _atomic_json(path, value):
    part = path.with_suffix(path.suffix + ".part")
    with part.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    part.replace(path)
