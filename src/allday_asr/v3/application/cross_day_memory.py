"""Explicit bounded cache projection, reused by Desktop and the existing sync owner."""

from datetime import datetime, timezone
from allday_asr.v3.domain.cross_day_memory import INDEX_ID, project
from allday_asr.v3.domain.models import CorrectionOperation
from allday_asr.v3.domain.ids import new_ulid


class CrossDayMemoryService:
    def __init__(self, sources, uow_factory, now=None):
        self.sources, self.uow_factory = sources, uow_factory
        self.now = now or (lambda: datetime.now(timezone.utc))

    def inventory(self, first, last, timezone="Asia/Singapore"):
        return self.sources.inventory(first, last, timezone)

    def update(self, first, last, contacts, timezone="Asia/Singapore"):
        if (
            not isinstance(contacts, list)
            or not 1 <= len(contacts) <= 3
            or len(set(contacts)) != len(contacts)
        ):
            raise ValueError("select one to three distinct contacts")
        with self.uow_factory().reading() as uow:
            scope, old = uow.cross_day_index.current()
        if scope and [
            scope["first"],
            scope["last"],
            scope["timezone"],
            [p["id"] for p in scope["contacts"]],
        ] == [first, last, timezone, contacts]:
            scope = self.sources.increment(scope)
            return self._publish(scope, self.sources.load(scope), old["revision"])
        scope = self.sources.freeze(first, last, timezone, contacts)
        return self._publish(
            scope, self.sources.load(scope), old["revision"] if old else 0
        )

    def get(self):
        with self.uow_factory().reading() as uow:
            scope, old = uow.cross_day_index.current()
        if not scope:
            return {
                "status": "not_selected",
                "id": INDEX_ID,
                "revision": 0,
                "people": [],
                "discussions": [],
                "things": [],
                "topics": [],
                "candidates": [],
            }
        return self._publish(scope, self.sources.load(scope), old["revision"])

    def refresh_saved(self):
        return self.get()

    def _publish(self, scope, rows, expected):
        with self.uow_factory() as uow:
            current_scope, old = uow.cross_day_index.current()
            if (old["revision"] if old else 0) != expected:
                raise ValueError("memory input changed; reload before updating")
            if self.sources.load(scope) != rows:
                raise ValueError(
                    "memory sources changed during publication; retry bounded update"
                )
            value = project(scope, rows, uow.cross_day_index.decisions())
            value["status"] = "ready" if value["discussions"] else "no_data"
            if (
                old
                and {
                    k: v for k, v in old.items() if k not in {"revision", "updated_at"}
                }
                == value
                and current_scope == scope
            ):
                return old
            value["revision"], value["updated_at"] = (
                expected + 1,
                self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            uow.cross_day_index.save(scope, value, value["updated_at"])
            uow.changes.append(
                "memory_index", INDEX_ID, value["revision"], "upsert", value
            )
            return value

    def decide(
        self,
        source,
        target,
        action,
        expected_revision,
        actor="desktop-user",
        operation_id=None,
    ):
        self.get()  # Validate current sources before accepting any human association.
        with self.uow_factory() as uow:
            return apply_decision(
                uow,
                source,
                target,
                action,
                expected_revision,
                self.now(),
                actor,
                operation_id,
            )


def apply_decision(
    uow, source, target, action, expected_revision, now, actor, operation_id=None
):
    scope, old = uow.cross_day_index.current()
    if (
        old is None
        or type(expected_revision) is not int
        or expected_revision != old["revision"]
    ):
        raise ValueError("memory revision conflict; synchronize and reload")
    rows = {r["id"]: r for r in old["discussions"]}
    if (
        action not in {"link", "reject", "detach"}
        or source not in rows
        or target not in rows
        or source == target
    ):
        raise ValueError("invalid memory association")
    if action == "link":
        if (
            rows[source]["evidence_status"] != "valid"
            or rows[target]["evidence_status"] != "valid"
        ):
            raise ValueError("invalid evidence cannot confirm an association")
        if rows[source]["thing_id"] == rows[target]["thing_id"]:
            return old
        # Only move an independent record, never a whole merged group implicitly.
        if sum(r["thing_id"] == rows[source]["thing_id"] for r in rows.values()) > 1:
            raise ValueError("detach this record before moving it to another thing")
    decisions = uow.cross_day_index.decisions()
    if any(
        d == {"source": source, "target": target, "action": action} for d in decisions
    ):
        return old
    uow.cross_day_index.decide(source, target, action)
    value = project(scope, list(rows.values()), uow.cross_day_index.decisions())
    value["excluded"] = old.get("excluded", [])
    value.update(
        status=old["status"], revision=old["revision"] + 1, updated_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    uow.cross_day_index.save(scope, value, value["updated_at"])
    uow.corrections.add(
        CorrectionOperation(
            operation_id or new_ulid(),
            "memory_index",
            INDEX_ID,
            expected_revision,
            {"source": source, "target": target, "action": action},
            actor,
            now,
            None,
        )
    )
    uow.changes.append("memory_index", INDEX_ID, value["revision"], "upsert", value)
    return value


def apply_memory_operation(operation, uow, now):
    from allday_asr.v3.domain.device_sync import OperationReceipt, ClientOperationStatus

    try:
        if set(operation.payload) != {"source", "target", "action"}:
            raise ValueError("invalid memory command")
        with uow.savepoint():
            value = apply_decision(
                uow,
                **operation.payload,
                expected_revision=operation.base_revision,
                now=now,
                actor="phone-user",
                operation_id=operation.operation_id,
            )
    except (ValueError, KeyError, TypeError):
        return OperationReceipt(
            operation.operation_id,
            ClientOperationStatus.CONFLICT,
            None,
            {
                "code": "MEMORY_REVISION_CONFLICT",
                "message": "关联或证据已变化；请同步后核对，原人工决定保留",
                "retryable": False,
            },
        )
    return OperationReceipt(
        operation.operation_id,
        ClientOperationStatus.APPLIED,
        value["revision"],
        None,
        ({"resource_id": INDEX_ID, "revision": value["revision"]},),
    )
