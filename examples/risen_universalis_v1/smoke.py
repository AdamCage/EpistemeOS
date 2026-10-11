"""Local mechanical smoke for risen_universalis_v1. Not a scientific study.

Two invented rows, two frozen contrasts, one selected experiment, two primary
runs and their independent reanalyses, then a human review assignment and a
mechanical package. No GPU, no network API, no reviewer approval, no publication.

    python examples/risen_universalis_v1/smoke.py --root <empty-directory>

Requires uv on PATH for ``uv_locked_python_v2``. The run is cheap. The protocol
checkpoint says a human approval would be required before an expensive run; the
checkpoint itself is not that approval. A matching digest is not scientific success.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from uuid import uuid4

from episteme.analysis_controller import advance_pack_analysis
from episteme.batch_controller import advance_batch
from episteme.commands import CommandService
from episteme.domain_packs import execution_fields
from episteme.domains import registry
from episteme.domains.packs import risen_universalis_v1 as pack
from episteme.execution import freeze_environment
from episteme.execution_locked import freeze_closure
from episteme.kernel import Actor, Kernel
from episteme.package import write_package
from episteme.planning import Planning
from episteme.reproduction import reproduce
from episteme.search import COMPONENTS, Search
from episteme.store import Store


STUDY = "risen-universalis-smoke"
PLANNER = Actor("risen-planner", "planner")
ANALYST = Actor("risen-analyst", "analyst")
REVIEWER = "risen-reviewer"
SCOPE = {"population": "declared smoke rows", "dataset": pack.FIXTURE_ID}


def _command(store: Store, action: str, payload: dict, *, actor: Actor = PLANNER):
    return CommandService(store).execute(dict(
        context=dict(command_id=uuid4().hex, expected_revision=len(store.events()),
                     actor=actor.id, role=actor.role, study_id=STUDY,
                     correlation_id="risen-smoke", causation_id=None),
        request=dict(version=1, action=action, payload=payload)))


def _checkpoint(store: Store, directory: Path, milestone: str, *, evidence: dict, note: str) -> dict:
    """Write one digest outside the store. Success of this write is not scientific success."""
    directory.mkdir(parents=True, exist_ok=True)
    resolved = directory.resolve()
    root = store.root.resolve()
    if resolved == root or root in resolved.parents or resolved in root.parents:
        raise RuntimeError("checkpoint directory must sit outside the store")
    history, _receipts = store.snapshot()
    digest = store.external_checkpoint()
    record = {
        "format": "episteme-operator-checkpoint-v1",
        "milestone": milestone,
        "scientific_success": False,
        "digest": digest,
        "revision": len(history),
        "head": history[-1]["hash"] if history else "0" * 64,
        "evidence": evidence,
        "note": note,
    }
    path = directory / f"{milestone}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return record


def _preregister(store: Store, explanation: str, environment: str, contrast: str) -> dict:
    loaded = registry.load_pack(pack.PACK_ID)
    return _command(store, "pack.preregister", dict(
        explanation_set=explanation, pack_id=pack.PACK_ID, pack_version=loaded.pack_version,
        pack_code_digest=loaded.pack_code_digest, parameters={"contrast": contrast},
        host_inputs={"fixture_id": pack.FIXTURE_ID}, capture=None, environment=environment))


def run_smoke(root: Path) -> dict:
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("smoke root must be an empty or new directory")
    checkpoints = root.parent / (root.name + "-checkpoints")
    package_dir = root.parent / (root.name + "-package")
    with Store(root) as store:
        plan = Planning(store, PLANNER)
        question = plan.question(
            study_id=STUDY,
            statement="On the declared smoke rows, is the paired gap score_a minus score_b?",
            objective="Exercise risen_universalis_v1 without a real study, GPU, or API",
            scope=SCOPE, constraints=["Invented rows only", "No hosted model"],
            stopping_criteria=["Two primary runs and their reanalyses, then stop for a human"])
        kernel = Kernel(store, PLANNER)
        hypotheses = [kernel.hypothesis(text, prediction, falsifier, SCOPE)
                      for text, prediction, falsifier in (
                          ("The paired gap equals score_a minus score_b",
                           "The recomputed gap matches that difference",
                           "The recomputed gap differs"),
                          ("The paired gap does not equal score_a minus score_b",
                           "The recomputed gap differs",
                           "The recomputed gap matches that difference"))]
        explanation = plan.explanation_set(
            question=question, hypotheses=hypotheses,
            comparison_plan="Keep both explanations. Select the paired contrast; do not rewrite either.")
        explanation_event = Kernel._get(store.events(), explanation, "explanation_set")
        frozen_question = _checkpoint(
            store, checkpoints, "question_frozen",
            evidence={"research_question": question, "explanation_set": explanation,
                      "explanation_set_hash": explanation_event["hash"],
                      "hypotheses": hypotheses},
            note=("Заморожены вопрос и ExplanationSet. Гипотезы после этого не переписываются молча: "
                  "новая версия была бы новым событием, старое остаётся. Этот digest не научный успех."))
        before = len(store.events())
        try:
            _preregister(store, explanation, freeze_environment(store), "paired_difference")
        except ValueError as exc:
            if "environment" not in str(exc):
                raise
        else:
            raise RuntimeError("trusted_local_python_v1 was accepted")
        if len(store.events()) != before:
            raise RuntimeError("rejecting v1 wrote events")
        environment = freeze_closure(store, pack.project(), set_variables=pack.VARIABLES)
        alternative = _preregister(store, explanation, environment, "sum_of_scores")
        chosen = _preregister(store, explanation, environment, "paired_difference")
        search = Search(store, PLANNER)
        tree = search.register_tree(
            weights={key: 1 for key in COMPONENTS}, cost_weight=0, budget=4,
            cost_unit="enqueued_attempt", max_nodes=2, max_depth=0, max_width=2,
            max_selections=1, max_retries=0)
        components = dict(discrimination=0, uncertainty=0, coverage=1, invalidity_risk=0.5)
        search.add_node(tree, protocol=alternative["protocol"], action="discriminate",
                        estimated_cost=4, components=components,
                        rationale="Competing sum contrast. Preserved and not selected. Not a result.")
        search.add_node(tree, protocol=chosen["protocol"], action="discriminate", estimated_cost=4,
                        components=dict(discrimination=1, uncertainty=0, coverage=1, invalidity_risk=0),
                        rationale="Paired gap discriminates the two frozen explanations. Priority is not truth.")
        selection = search.select_next(tree)
        if selection.get("protocol") != chosen["protocol"]:
            raise RuntimeError("discriminating selection did not keep the paired contrast")
        protocol_event = Kernel._get(store.events(), chosen["protocol"], "protocol")
        frozen_protocol = _checkpoint(
            store, checkpoints, "protocol_before_expensive_run",
            evidence={"selected_protocol": chosen["protocol"],
                      "selected_protocol_hash": protocol_event["hash"],
                      "unselected_protocol": alternative["protocol"],
                      "explanation_set_hash": explanation_event["hash"]},
            note=("Протокол и StatisticalDesign заморожены до запусков. Дымовой прогон дешёвый. "
                  "Перед дорогим запуском здесь нужно отдельное человеческое одобрение. "
                  "Эта запись таким одобрением не является и научным успехом не является."))
        binding = Kernel._get(store.events(), chosen["binding"], "pack_binding")
        if binding["payload"]["execution_profile"] != pack.PROFILE:
            raise RuntimeError("selected study is not pinned to uv_locked_python_v2")
        batch = _command(store, "batch.plan", dict(
            selection=selection["id"], executor="risen-executor", replicator="risen-reanalyst",
            **execution_fields(store, binding)))
        settled = advance_batch(store, batch)
        if settled["status"] != "completed":
            raise RuntimeError(f"smoke batch did not complete: {settled['status']}")
        runs = [event for event in store.events() if event["kind"] == "run"
                and event["payload"]["protocol"] == chosen["protocol"]]
        primary_runs = [event for event in runs if event["payload"]["replicate_of"] is None]
        if len(primary_runs) != 2 or len(runs) != 4:
            raise RuntimeError("expected two primary runs and two reanalyses")
        if any(event["kind"] == "run" and event["payload"].get("protocol") == alternative["protocol"]
               for event in store.events()):
            raise RuntimeError("the unselected contrast was executed")
        replay = reproduce(store, primary_runs[0]["id"])
        if replay["counts_as_evidence"] or replay["replay"] != "same_code_fresh_environment":
            raise RuntimeError("same-code replay was treated as evidence")
        analysed = advance_pack_analysis(store, batch, planner=PLANNER, analyst=ANALYST,
                                         reviewer_actor=REVIEWER)
        claim = Kernel._get(store.events(), analysed["claim"], "claim")
        gate = Kernel(store, Actor("risen-observer", "observer")).gate(analysed["claim"])
        if (claim["payload"]["outcome"], claim["payload"]["inference_mode"],
                gate["scientific_validity"], analysed["scientific_validity"]) != (
                    "inconclusive", "exploratory", "not_assessed", "not_assessed"):
            raise RuntimeError("smoke claim was promoted")
        if claim["payload"].get("scientific_validity") not in (None, "not_assessed"):
            raise RuntimeError("claim payload raised scientific_validity")
        analysis = Kernel._get(store.events(), analysed["analysis"], "pack_analysis")
        if analysis["payload"]["ceiling"]["max_inference_mode"] == "confirmatory":
            raise RuntimeError("confirmatory strength was manufactured")
        if analysis["payload"]["replication"]["mode"] != "same_data_reanalysis":
            raise RuntimeError("reanalysis was relabeled as replication")
        frozen_claim = _checkpoint(
            store, checkpoints, "claim_ready_for_human_review",
            evidence={"claim": analysed["claim"], "claim_hash": claim["hash"],
                      "assignment": analysed["assignment"], "basis_hash": analysed["basis_hash"]},
            note=("Claim готов к научному review. Reviewer назначен. Одобрение не выставлено и "
                  "моделью не создаётся. scientific_validity остаётся not_assessed. "
                  "Успех checkpoint научным успехом не является."))
        package = write_package(store, analysed["claim"], package_dir)
        if package["venue_submission"] or package["events_appended"] != 0:
            raise RuntimeError("package wrote events or claimed a submission")
        frozen_package = _checkpoint(
            store, checkpoints, "mechanical_package_not_a_scientific_release",
            evidence={"claim": analysed["claim"], "package": str(package_dir),
                      "snapshot_hash": package["snapshot_hash"]},
            note=("Собран механический каталог воспроизведения. Это не научная публикация и не подача. "
                  "reproduce не является replication. scientific_validity не повышалась."))
        if any(event["kind"] in {"review", "review_submission", "paper"} for event in store.events()):
            raise RuntimeError("a review verdict or paper was fabricated")
        still = Kernel._get(store.events(), explanation, "explanation_set")
        if still["hash"] != explanation_event["hash"]:
            raise RuntimeError("the frozen explanation set was rewritten")
        return {"question": frozen_question, "protocol": frozen_protocol, "claim": frozen_claim,
                "package": frozen_package, "claim_id": analysed["claim"],
                "scientific_validity": "not_assessed"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Mechanical risen_universalis_v1 smoke. Not a study.")
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_smoke(args.root), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
