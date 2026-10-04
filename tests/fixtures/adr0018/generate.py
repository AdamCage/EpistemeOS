"""Build ADR 0018 historical fixture stores with the code of audit commit da6aa2a.

Usage, from a ``git archive da6aa2a`` snapshot (never from the current tree):

    set PYTHONPATH=<snapshot>\\src;<snapshot>\\tests
    python generate.py <output-directory>

Every opinion here is an explicit synthetic fixture recorded through paths that
code at da6aa2a accepted; none is a scientific review. The stores exist so that
current code can be checked against histories it would refuse to write today.
"""

from __future__ import annotations

import inspect
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from episteme.batch import _index as batch_index
from episteme.commands import CommandService
from episteme.demo import run_demo
from episteme.kernel import Actor, Kernel
from episteme.reporting import PaperBuilder
from episteme.reviewer_controller import ReviewerController
from episteme.store import Store, canonical

import golden_support
import test_batch_analysis

RATIONALE = "Explicit historical fixture opinion; no scientific review was performed"
PLANNER, PROVIDER, STUDY = "fixture-review-planner", "fixture-review-provider", "fixture-review-study"


class Provider:
    def __init__(self, decision):
        self.decision = decision

    def invoke(self, request):
        return canonical(dict(schema_version=1, assignment=request["assignment"],
                              bundle=request["bundle"], claim=request["claim"],
                              basis_hash=request["basis_hash"],
                              reviewer_actor=request["reviewer_actor"], **self.decision)), {}


def _command(store, actor, role, action, payload, study):
    return CommandService(store).execute(dict(
        context=dict(command_id=f"{action}-{uuid4().hex}", expected_revision=len(store.events()),
                     actor=actor, role=role, study_id=study,
                     correlation_id="adr0018-historical-fixture", causation_id=None),
        request=dict(version=1, action=action, payload=payload)))


def _basis(store, claim):
    return Kernel(store, Actor("fixture-reader", "observer")).gate(claim)["basis_hash"]


def _submitted_approval(store, claim, reviewer, study):
    basis = _basis(store, claim)
    assignment = _command(store, PLANNER, "planner", "review.assign",
                          dict(claim=claim, reviewer_actor=reviewer, expected_basis=basis),
                          study)["assignment"]
    delivered = ReviewerController(store, Actor(PLANNER, "planner"), study, PROVIDER, Provider(
        dict(verdict="approve", rationale=RATIONALE, findings=[], link_assessments=None))
    ).advance(assignment)
    assert delivered["status"] == "completed", delivered
    return _command(store, reviewer, "reviewer", "review.submit",
                    dict(assignment=assignment, response=delivered["response"],
                         expected_basis=basis), study)


def _demo_claim(store):
    return next(e["id"] for e in store.events() if e["kind"] == "claim")


def legacy_review_paper(root: Path) -> dict:
    """A-06: CLI-equivalent kernel.review approval of the demo claim, then paper.build."""
    run_demo(root)
    with Store(root) as store:
        claim = _demo_claim(store)
        basis = _basis(store, claim)
        Kernel(store, Actor("fixture-legacy-reviewer", "reviewer")).review(
            claim, verdict="approve", rationale=RATIONALE, actions=[], expected_basis=basis)
        PaperBuilder(store, Actor("fixture-writer", "writer")).build(
            title="Historical fixture paper", claims=[claim], expected_bases={claim: basis})
        return golden_support.export_history(store)


def noncanonical_reviewer(root: Path) -> dict:
    """A-22: an assigned approval by a case variant of the demo executor ID."""
    run_demo(root)
    with Store(root) as store:
        _submitted_approval(store, _demo_claim(store), "Fixture-Executor", STUDY)
        return golden_support.export_history(store)


def forged_v1_analysis() -> dict:
    """A-04: a schema 1 analysis whose proposal the adapter did not compute, then approved."""
    fixture = test_batch_analysis.BatchAnalysisTests(
        methodName="test_full_batch_to_frozen_claim_assignment_and_restart")
    fixture.setUp()
    try:
        store = fixture.store
        batch = fixture._complete()
        state = batch_index(store, store.events())[batch]
        honest = fixture.adapter.propose(store, state)
        forged = dict(honest, outcome="supports",
                      statement="The treatment causes the outcome in this population.")
        source = store.put(Path(inspect.getfile(type(fixture.adapter))).read_bytes())
        study = fixture.fixture.study
        applied = _command(store, fixture.analyst.id, "analyst", "analysis.apply", dict(
            batch=batch, expected_settlement=state["settlement"]["id"], proposal=forged,
            adapter_source_digest=source, reviewer_actor=fixture.reviewer), study)
        _submitted_approval(store, applied["claim"], fixture.reviewer, study)
        return golden_support.export_history(store)
    finally:
        fixture.doCleanups()


def main(output: str) -> int:
    target = Path(output)
    target.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="episteme-adr0018-fixture-") as temporary:
        histories = dict(legacy_review_paper=legacy_review_paper(Path(temporary) / "paper"),
                         noncanonical_reviewer=noncanonical_reviewer(Path(temporary) / "case"),
                         forged_v1_analysis=forged_v1_analysis())
    for name, history in histories.items():
        (target / f"{name}.json").write_bytes(canonical(history) + b"\n")
        print(name, len(history["events"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
