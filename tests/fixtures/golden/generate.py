"""Regenerate the saved golden fixture histories; run only deliberately.

Usage from the repository root, with an unmodified ``src`` tree:

    $env:PYTHONPATH = "src;tests"; python tests/fixtures/golden/generate.py

Every history is a synthetic fixture: local subprocess jobs, caller-declared
actors, no provider call, no reviewer verdict. New output has new IDs and
timestamps, so the expectations must be regenerated together with the data
and reviewed as a deliberate change of the golden baseline.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]

from episteme.analysis_controller import advance_batch_analysis  # noqa: E402
from episteme.batch_controller import advance_batch  # noqa: E402
from episteme.demo import run_demo  # noqa: E402
from episteme.domains.synthetic_batch_analysis import SyntheticCausalBatchAnalysisAdapter  # noqa: E402
from episteme.kernel import Actor  # noqa: E402
from episteme.search import Search  # noqa: E402
from episteme.store import Store, canonical  # noqa: E402

import golden_support  # noqa: E402
import test_domain_binding  # noqa: E402
import test_proposal_execution  # noqa: E402


def _model_path() -> dict:
    fixture = test_proposal_execution.ProposalExecutionTests(
        methodName="test_selected_model_node_and_frozen_sources_share_one_receipt")
    fixture.setUp()
    try:
        batch = fixture.command("proposal.prepare_next", fixture.prepare()["request"]["payload"])
        if advance_batch(fixture.store, batch)["status"] != "completed":
            raise RuntimeError("model-path fixture batch did not complete")
        advance_batch_analysis(fixture.store, batch, planner=fixture.planner,
                               analyst=Actor("golden-analyst", "analyst"),
                               reviewer_actor="golden-reviewer",
                               adapter=SyntheticCausalBatchAnalysisAdapter())
        return golden_support.export_history(fixture.store)
    finally:
        fixture.doCleanups()


def _manual_binding() -> dict:
    fixture = test_domain_binding.DomainBindingTests(
        methodName="test_manual_recipe_to_completed_batch_claim_and_review_assignment")
    fixture.setUp()
    try:
        fixture.bind()
        selection = Search(fixture.store, fixture.planner).select_next(fixture.tree)["id"]
        batch = fixture.command("batch.plan", dict(selection=selection,
            executor="manual-executor", replicator="manual-reanalyst", **fixture.execution))
        if advance_batch(fixture.store, batch)["status"] != "completed":
            raise RuntimeError("manual-binding fixture batch did not complete")
        advance_batch_analysis(fixture.store, batch, planner=fixture.planner,
                               analyst=fixture.analyst, reviewer_actor="manual-reviewer",
                               adapter=fixture.adapter)
        return golden_support.export_history(fixture.store)
    finally:
        fixture.doCleanups()


def _legacy_demo() -> dict:
    with TemporaryDirectory(prefix="episteme-golden-demo-") as temporary:
        root = Path(temporary) / "demo"
        run_demo(root, with_search=True)
        with Store(root) as store:
            return golden_support.export_history(store)


def main() -> int:
    status = subprocess.run(["git", "status", "--porcelain", "--", "src"], cwd=REPO,
                            capture_output=True, text=True, check=True).stdout
    if status.strip():
        raise SystemExit("golden fixtures must be generated from an unmodified src tree")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                            text=True, check=True).stdout.strip()
    expected = dict(generated_from=dict(commit=commit, src_tree="unmodified",
                                        python=sys.version.split()[0]),
                    stores={}, legacy_synthetic=golden_support.legacy_synthetic_recipes())
    for name, build in (("model_path", _model_path), ("manual_binding", _manual_binding),
                        ("legacy_demo", _legacy_demo)):
        history = build()
        path = golden_support.GOLDEN / f"{name}.json"
        path.write_bytes(canonical(history) + b"\n")
        with TemporaryDirectory(prefix="episteme-golden-check-") as temporary:
            store = golden_support.load_history(history, Path(temporary) / "loaded")
            try:
                expected["stores"][name] = golden_support.observe(store)
            finally:
                store.close()
    (golden_support.GOLDEN / "expected.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({name: dict(revision=row["revision"], head=row["head"])
                      for name, row in expected["stores"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
