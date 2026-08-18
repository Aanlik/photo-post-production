"""Explicit, per-photo state transitions for failure-isolated batch work."""

from __future__ import annotations


_ALLOWED_TARGETS = {
    "queued": {"analyzing", "failed"},
    "analyzing": {"analyzed", "failed"},
    "analyzed": {"clustered", "failed"},
    "clustered": {"scored", "failed"},
    "scored": {"selected", "review", "rejected", "failed"},
    "selected": {"editing", "completed", "failed"},
    "review": {"selected", "rejected", "failed"},
    "rejected": {"completed"},
    "editing": {"edited", "failed"},
    "edited": {"completed"},
    "completed": set(),
    "failed": {"queued"},
}


def transition_photo_state(photo_id: str, current: str, target: str) -> dict:
    """Describe a valid transition without changing another photo's state."""
    accepted = target in _ALLOWED_TARGETS.get(current, set())
    result = {
        "photo_id": photo_id,
        "previous_state": current,
        "state": target if accepted else current,
        "accepted": accepted,
    }
    if not accepted:
        result["error"] = f"invalid transition from {current!r} to {target!r}"
    return result
