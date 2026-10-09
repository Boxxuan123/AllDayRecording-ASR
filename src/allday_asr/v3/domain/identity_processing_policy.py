"""Recognition is a product operation; learning permission never substitutes for truth."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IdentityProcessingPolicy:
    recognition_enabled: bool
    profile_learning_allowed: bool
    annotation_queue_allowed: bool

    @classmethod
    def for_session(cls, role, *, annotation_only=False):
        if role not in {"learning", "blind", "holdout"}:
            raise ValueError("session dataset role is unassigned")
        return cls(not annotation_only, role == "learning", role == "learning")
