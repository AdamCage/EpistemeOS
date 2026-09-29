"""Durable, context-projected delivery to a caller-supplied review provider.

The provider receives only the serialized request passed to ``invoke``. A
provider running under the same OS identity can still read the Store directly;
this module does not authenticate actors or enforce filesystem/network access.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Protocol
from uuid import uuid4

from .kernel import Actor, Kernel, require
from .review_assignment import _index as assignment_index
from .store import Store, canonical, digest


MAX_CONTEXT_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 48 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ARTIFACTS = 256
_DISPATCH_REQUEST = {"assignment", "provider_id"}
_FINALIZE_REQUEST = {"assignment", "response", "status", "usage"}
_DISPATCH_FIELDS = {"schema_version", "assignment", "assignment_hash", "bundle",
                    "claim", "basis_hash", "reviewer_actor", "provider_id", "request",
                    "identity_assurance", "read_isolation"}
_RESPONSE_FIELDS = {"schema_version", "assignment", "dispatch", "dispatch_hash",
                    "bundle", "claim", "basis_hash", "reviewer_actor", "response",
                    "status", "usage", "identity_assurance", "read_isolation"}


class ReviewProvider(Protocol):
    def invoke(self, request: dict[str, Any]) -> tuple[bytes, dict[str, int | float]]:
        """Return raw response bytes and observed usage; no Store handle is passed."""


def _usage(value: Any) -> dict[str, int | float]:
    require(type(value) is dict and len(value) <= 64, "review usage must be a bounded object")
    require(all(type(key) is str and bool(key.strip()) and len(key) <= 128
                and type(amount) in (int, float) and amount >= 0 and amount < float("inf")
                for key, amount in value.items()), "review usage needs finite nonnegative numbers")
    return value


def _assignment_request(store: Store, history: list[dict[str, Any]], assignment: str,
                        provider_id: str, *, receipts: list[dict[str, Any]] | None = None
                        ) -> tuple[dict[str, Any], dict[str, Any]]:
    require(type(assignment) is str and bool(assignment.strip()), "assignment ID is required")
    require(type(provider_id) is str and bool(provider_id.strip()) and len(provider_id) <= 256,
            "provider_id must be bounded nonempty text")
    assignments = assignment_index(store, history, receipts=receipts)
    require(assignment in assignments, "unknown verified review assignment")
    event = assignments[assignment]
    p = event["payload"]
    kernel = Kernel(store, Actor("review-delivery-validator", "observer"))
    gate = kernel._gate(history, p["claim"])
    basis, _ = kernel._basis(history, p["claim"])
    require(gate["passed"] and gate["basis_hash"] == basis == p["basis_hash"],
            "review dispatch needs the assigned current evidence basis")
    manifest = json.loads(store.read(p["bundle"]))
    keys = manifest["allowed_artifact_digests"]
    require(type(keys) is list and len(keys) <= MAX_ARTIFACTS and keys == sorted(set(keys)),
            "invalid review artifact allowlist")
    artifacts = []
    size = 0
    for key in keys:
        require(type(key) is str, "invalid review artifact digest")
        raw = store.read(key)
        size += len(raw)
        require(size <= MAX_CONTEXT_BYTES, "review context exceeds delivery byte limit")
        artifacts.append(dict(digest=key, data_base64=base64.b64encode(raw).decode("ascii")))
    request = dict(schema_version=1, assignment=assignment, assignment_hash=event["hash"],
                   bundle=p["bundle"], claim=p["claim"], basis_hash=p["basis_hash"],
                   reviewer_actor=p["reviewer_actor"], provider_id=provider_id,
                   context=manifest, allowed_artifacts=artifacts,
                   identity_assurance="caller_declared", read_isolation="not_enforced")
    require(len(canonical(request)) <= MAX_REQUEST_BYTES,
            "serialized review provider request exceeds byte limit")
    return event, request


def _dispatch_payload(assignment: dict[str, Any], request: dict[str, Any],
                      request_digest: str) -> dict[str, Any]:
    return dict(schema_version=1, assignment=assignment["id"],
                assignment_hash=assignment["hash"], bundle=request["bundle"],
                claim=request["claim"], basis_hash=request["basis_hash"],
                reviewer_actor=request["reviewer_actor"],
                provider_id=request["provider_id"], request=request_digest,
                identity_assurance="caller_declared", read_isolation="not_enforced")


def _response_payload(dispatch: dict[str, Any], response: str | None,
                      status: str, usage: dict[str, int | float]) -> dict[str, Any]:
    p = dispatch["payload"]
    return dict(schema_version=1, assignment=p["assignment"], dispatch=dispatch["id"],
                dispatch_hash=dispatch["hash"], bundle=p["bundle"], claim=p["claim"],
                basis_hash=p["basis_hash"], reviewer_actor=p["reviewer_actor"],
                response=response, status=status, usage=usage,
                identity_assurance="caller_declared", read_isolation="not_enforced")


def _index(store: Store, history: list[dict[str, Any]], *,
           receipts: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Replay-check every dispatch/response and its original command receipt."""
    source = store.receipts() if receipts is None else receipts
    relevant = [r for r in source if r["request"]["action"] in {
        "review.dispatch", "review.finalize"} and r["after_revision"] <= len(history)]
    dispatch_events = {e["id"] for e in history if e["kind"] == "review_dispatch"}
    response_events = {e["id"] for e in history if e["kind"] == "review_response"}
    states: dict[str, dict[str, Any]] = {}
    seen_dispatch: set[str] = set()
    seen_response: set[str] = set()
    for receipt in relevant:
        context, request = receipt["context"], receipt["request"]
        require(context["role"] == "planner" and request["version"] == 1,
                "review delivery needs a planner v1 command")
        args = request["payload"]
        before = history[:receipt["before_revision"]]
        events = history[receipt["before_revision"]:receipt["after_revision"]]
        require(len(events) == 1 and receipt["event_ids"] == [events[0]["id"]],
                "review delivery requires an exact single-event receipt")
        event = events[0]
        require(event["actor"] == context["actor"] and event["role"] == "planner",
                "review delivery event actor differs from its command")
        if request["action"] == "review.dispatch":
            require(set(args) == _DISPATCH_REQUEST and event["kind"] == "review_dispatch",
                    "review dispatch receipt has wrong arguments or event kind")
            assignment, frozen = _assignment_request(store, before, args["assignment"],
                                                      args["provider_id"], receipts=source)
            require(assignment["payload"]["study_id"] == context["study_id"],
                    "review dispatch study differs from assignment")
            request_digest = digest(canonical(frozen))
            require(store.read(request_digest) == canonical(frozen),
                    "review provider request differs from its frozen projection")
            require(set(event["payload"]) == _DISPATCH_FIELDS
                    and event["payload"] == _dispatch_payload(assignment, frozen, request_digest)
                    and receipt["result"] == dict(dispatch=event["id"], request=request_digest),
                    "review dispatch differs from its frozen context")
            require(assignment["id"] not in states,
                    "assignment has more than one review dispatch")
            states[assignment["id"]] = dict(assignment=assignment, dispatch=event,
                                             response=None, request=request_digest)
            seen_dispatch.add(event["id"])
        else:
            require(set(args) == _FINALIZE_REQUEST and event["kind"] == "review_response",
                    "review finalize receipt has wrong arguments or event kind")
            require(args["assignment"] in states and states[args["assignment"]]["response"] is None,
                    "review response needs one prior dispatch")
            state = states[args["assignment"]]
            dispatch = state["dispatch"]
            require(dispatch["seq"] <= len(before)
                    and state["assignment"]["payload"]["study_id"] == context["study_id"],
                    "review response differs from dispatch or study")
            require(args["status"] in {"completed", "failed"}
                    and (type(args["response"]) is str if args["status"] == "completed"
                         else args["response"] is None or type(args["response"]) is str),
                    "invalid review provider completion")
            if args["response"] is not None:
                require(len(store.read(args["response"])) <= MAX_RESPONSE_BYTES,
                        "review provider response exceeds byte limit")
            usage = _usage(args["usage"])
            require(set(event["payload"]) == _RESPONSE_FIELDS
                    and event["payload"] == _response_payload(dispatch, args["response"],
                                                               args["status"], usage)
                    and receipt["result"] == dict(response_event=event["id"],
                                                  status=args["status"]),
                    "review response differs from its dispatch or raw bytes")
            state["response"] = event
            seen_response.add(event["id"])
    require(seen_dispatch == dispatch_events and seen_response == response_events,
            "review delivery event lacks its original command receipt")
    return states


class ReviewSession:
    """Planner-owned durable records around an external reviewer call."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def dispatch(self, *, assignment: str, provider_id: str) -> dict[str, str]:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "review dispatch requires a planner CommandService transaction")
        history = self.store.events()
        require(assignment not in _index(self.store, history), "review assignment already dispatched")
        source, request = _assignment_request(self.store, history, assignment, provider_id)
        require(source["payload"]["study_id"] == self.store._command_context["study_id"],
                "review dispatch study differs from assignment")
        request_digest = self.store.put_json(request)
        id = f"review_dispatch-{uuid4().hex[:16]}"
        self.store.append(id=id, kind="review_dispatch", actor=self.actor.id, role="planner",
                          payload=_dispatch_payload(source, request, request_digest),
                          expected_revision=len(history))
        return dict(dispatch=id, request=request_digest)

    def finalize(self, *, assignment: str, response: str | None,
                 status: str, usage: dict[str, int | float]) -> dict[str, str]:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "review finalize requires a planner CommandService transaction")
        history = self.store.events()
        states = _index(self.store, history)
        require(assignment in states and states[assignment]["response"] is None,
                "review finalize needs a dispatched, unfinalized assignment")
        state = states[assignment]
        require(state["assignment"]["payload"]["study_id"] == self.store._command_context["study_id"],
                "review finalize study differs from assignment")
        require(status in {"completed", "failed"}
                and (type(response) is str if status == "completed"
                     else response is None or type(response) is str),
                "invalid review provider completion")
        if response is not None:
            require(len(self.store.read(response)) <= MAX_RESPONSE_BYTES,
                    "review provider response exceeds byte limit")
        usage = _usage(usage)
        id = f"review_response-{uuid4().hex[:16]}"
        self.store.append(id=id, kind="review_response", actor=self.actor.id, role="planner",
                          payload=_response_payload(state["dispatch"], response, status, usage),
                          expected_revision=len(history))
        return dict(response_event=id, status=status)


class ReviewerController:
    """One-shot provider call; uncertain dispatch is never silently repeated."""

    def __init__(self, store: Store, planner: Actor, study_id: str,
                 provider_id: str, provider: ReviewProvider):
        require(planner.role == "planner", "review controller needs a planner actor")
        self.store, self.planner = store, planner
        self.study_id, self.provider_id, self.provider = study_id, provider_id, provider

    def _command(self, action: str, payload: dict[str, Any], causation: str) -> Any:
        from .commands import CommandService
        history = self.store.events()
        envelope = dict(context=dict(command_id=f"{action}-{uuid4().hex}",
                                     expected_revision=len(history), actor=self.planner.id,
                                     role="planner", study_id=self.study_id,
                                     correlation_id=causation, causation_id=causation),
                        request=dict(version=1, action=action, payload=payload))
        return CommandService(self.store).execute(envelope)

    def advance(self, assignment: str) -> dict[str, Any]:
        states = _index(self.store, self.store.events())
        if assignment in states:
            response = states[assignment]["response"]
            return (dict(status="unknown", dispatch=states[assignment]["dispatch"]["id"])
                    if response is None else dict(status=response["payload"]["status"],
                                                   response_event=response["id"],
                                                   response=response["payload"]["response"]))
        admitted = self._command("review.dispatch",
                                 dict(assignment=assignment, provider_id=self.provider_id), assignment)
        # The request is immutable and projected solely from the assignment's
        # manifest and allowlisted CAS bytes. The provider itself is untrusted.
        request = json.loads(self.store.read(admitted["request"]))
        try:
            raw, usage = self.provider.invoke(request)
            require(type(raw) is bytes and len(raw) <= MAX_RESPONSE_BYTES,
                    "review provider must return bounded raw bytes")
            _usage(usage)
        except Exception:
            return dict(status="unknown", dispatch=admitted["dispatch"])
        response = self.store.put(raw)
        try:
            recorded = self._command("review.finalize",
                                     dict(assignment=assignment, response=response,
                                          status="completed", usage=usage), admitted["dispatch"])
        except Exception:
            return dict(status="unknown", dispatch=admitted["dispatch"], response=response)
        return dict(status="completed", response_event=recorded["response_event"], response=response)

    def reconcile(self, assignment: str, *, response: str | None,
                  status: str, usage: dict[str, int | float]) -> dict[str, str]:
        states = _index(self.store, self.store.events())
        require(assignment in states and states[assignment]["response"] is None,
                "only an unknown review dispatch can be reconciled")
        return self._command("review.finalize", dict(assignment=assignment, response=response,
                                                     status=status, usage=usage),
                             states[assignment]["dispatch"]["id"])
