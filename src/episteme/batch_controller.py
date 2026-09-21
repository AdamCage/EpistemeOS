"""Resume one frozen local batch; caller-declared roles, no AI or sandbox.

No external process runs inside a command transaction. Existing dispatches go
through the runner's reconciliation path and never grant new launch permission.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .batch import batch_state
from .commands import CommandService
from .execution import _receipt_for, work_job
from .execution_authority import require_authority
from .kernel import Kernel
from .store import ConflictError, Store


def advance_batch(store: Store, batch: str) -> dict[str, Any]:
    """Advance until settled or unknown; never fabricate analysis or review."""
    plan = Kernel._get(store.events(), batch, "batch_plan")
    original = _receipt_for(store, [batch])["context"]

    def command(action: str, payload: dict[str, Any], actor: str, role: str) -> Any:
        context = dict(original, command_id=f"batch-{uuid4().hex}", actor=actor, role=role,
                       expected_revision=state["revision"], causation_id=batch)
        return CommandService(store).execute(dict(context=context,
            request=dict(version=1, action=action, payload=payload)))

    while True:
        state = batch_state(store, batch)
        if state["settlement"] is not None:
            return state
        require_authority(store, plan["payload"]["execution_authority"])
        slot = next((item for item in state["slots"]
                     if item["status"] not in {"completed", "failed", "blocked_dependency"}), None)
        try:
            if slot is None:
                command("batch.settle", dict(batch=batch), plan["actor"], "planner")
            elif slot["job"] is None:
                command("batch.enqueue_slot", dict(batch=batch, slot=slot["slot"]),
                        slot["actor"], slot["role"])
            else:
                result = work_job(store, slot["job"])
                if result["status"] == "unknown":
                    return batch_state(store, batch)
        except ConflictError:
            # A concurrent admission won. Its receipt is evidence, never launch
            # permission for this controller; leave further advancement explicit.
            return batch_state(store, batch)
