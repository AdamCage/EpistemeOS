"""Prepare an offline, exploratory analysis of nine historical Afterlife traces.

The selected run already exists and its outcomes may have been inspected. This
example freezes that exposure, two separate local analysis programs, and a
complete batch before any EpistemeOS execution. It makes no provider call and
records no scientific review or paper approval.
"""

from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
from uuid import uuid4

from episteme.commands import CommandService
from episteme.domains import afterlife_seed
from episteme.domains.afterlife_seed_batch_analysis import (
    ADAPTER_ID, ADAPTER_VERSION, AfterlifeSeedBatchAnalysisAdapter,
)
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.search import COMPONENTS, Search
from episteme.store import Store


PLANNER = "afterlife-pilot-planner"
EXECUTOR = "afterlife-pilot-executor"
REANALYST = "afterlife-pilot-reanalyst"
ANALYST = "afterlife-pilot-analyst"
REVIEWER = "afterlife-pilot-reviewer"


def _command(store: Store, *, action: str, payload: dict, study: str,
             correlation: str, cause: str | None = None) -> str:
    return CommandService(store).execute({
        "context": {
            "command_id": f"afterlife-pilot-{uuid4().hex}",
            "expected_revision": len(store.events()),
            "actor": PLANNER,
            "role": "planner",
            "study_id": study,
            "correlation_id": correlation,
            "causation_id": cause,
        },
        "request": {"version": 1, "action": action, "payload": payload},
    })


def _study(store: Store, compiled: dict, source_run: Path) -> dict[str, str]:
    manifest = json.loads(store.read(compiled["manifest_digest"]))
    trajectories = manifest["trajectories"]
    seeds = compiled["seeds"]
    ids = compiled["trajectory_ids"]
    if (type(seeds) is not list or seeds != list(range(9))
            or type(ids) is not list or len(ids) != 9
            or len(set(ids)) != 9 or set(ids) != set(trajectories)
            or manifest.get("totals", {}).get("n_trajectories") != 9):
        raise ValueError("pilot requires the complete nine-trajectory historical roster")
    if compiled["metric"] != "stop_event_rate":
        raise ValueError("pilot requires the frozen stop_event_rate metric")

    run_id = manifest["run_id"]
    study = f"afterlife-historical-{run_id}"
    scope = {
        "domain": "afterlife_historical_trace_reanalysis",
        "legacy_run_id": run_id,
        "generator": manifest["config_resolved"]["generators"][0]["model_id"],
        "sample": "nine pre-existing trajectories in one selected run",
    }
    actor = Actor(PLANNER, "planner")
    kernel = Kernel(store, actor)
    planning = Planning(store, actor)
    search = Search(store, actor)

    # A caller declaration records prior exposure; it is not an access-control
    # audit or a retrospective claim that the old run was preregistered.
    exposure = kernel.expose_data(
        data=compiled["data"],
        purpose=("Historical source run and its nine trajectories were available "
                 "before this exploratory protocol was frozen."),
    )
    seen = sorted({compiled["data"], compiled["manifest_digest"],
                   compiled["config_digest"], *compiled["recipe_artifacts"]})
    question = planning.question(
        study_id=study,
        statement="Do the nine historical step traces reproduce their recorded stop-event rates?",
        objective="Audit one selected Afterlife run by offline, same-data reanalysis.",
        scope=scope,
        constraints=[
            "Historical data were observed before this protocol; all inference is exploratory.",
            "No model generation, network provider request, or new-data replication.",
            "The local actor IDs do not establish independent people or OS isolation.",
        ],
        stopping_criteria=[
            "Analyze each of the nine frozen traces once with both registered implementations; "
            "retain any failed or unknown attempts."
        ],
    )
    hypotheses = [kernel.hypothesis(statement, prediction, falsifier, scope)
                  for statement, prediction, falsifier in (
        (
            "The recorded stop-event rates are mechanically reproducible from the saved steps.",
            "Every trajectory's step count, stop count and rounded rate match its legacy manifest entry.",
            "At least one fully verified trajectory disagrees with the manifest.",
        ),
        (
            "At least one recorded rate reflects an incomplete or inconsistent saved trace.",
            "A verified trace disagrees with its manifest count or rounded rate.",
            "All nine verified traces reproduce the manifest counts and rounded rates.",
        ),
    )]
    explanation_set = planning.explanation_set(
        question=question,
        hypotheses=hypotheses,
        comparison_plan=("For each pre-existing trajectory, compare counts recomputed from "
                         "saved step records with the legacy manifest. Capture already "
                         "requires this agreement, so the batch repeats the consistency check "
                         "through the shared runner and cannot discriminate these explanations; "
                         "it does not test why stop behaviour varied."),
    )

    stopping_rule = (
        "Use exactly the nine already observed trajectories in the selected historical "
        "run, with no exclusions or interim selection. Execute one primary analysis "
        "and one same-data reanalysis per trajectory."
    )
    design = {
        "schema_version": 1,
        "mode": "exploratory",
        "experimental_unit": "historical trajectory",
        "estimand": ("Nine trajectory-specific stop-event fractions in the selected legacy run; "
                     "no population or causal estimand is claimed."),
        "primary_metric": {"name": compiled["metric"], "unit": "fraction"},
        "secondary_metrics": [],
        "sample_size": 9,
        "sample_size_rationale": ("All nine pre-existing trajectories in the selected run; "
                                  "the count was observed before this protocol and is not a power calculation."),
        "uncertainty": {
            "method": "not_applicable", "resampling_unit": None,
            "rationale": ("Only per-trajectory descriptive values are reported; no interval, "
                          "significance test or independence assumption is asserted."),
        },
        "exclusions": [],
        "stopping_rule": {"kind": "fixed_sample", "rule": stopping_rule},
        "multiple_testing": {
            "family": [], "correction": "not_applicable",
            "rationale": "No hypothesis-family error claim or significance test.",
        },
        "data_splits": [{
            "id": "historical_nine_open",
            "digest": compiled["data"],
            "role": "discovery",
            "exposure_policy": "open",
        }],
    }
    protocol = kernel.preregister_for_set(
        explanation_set=explanation_set,
        design=("Offline consistency check of nine saved Afterlife step traces; "
                "slot indices 0-8 identify existing trajectories, not fresh stochastic seeds."),
        metric=compiled["metric"],
        analysis_plan=("Count a stop event when finish_reason is neither null nor 'length'; "
                       "report the unrounded fraction and compare its four-decimal rounding "
                       "with the legacy manifest. Check the "
                       "full saved-step roster against the legacy manifest, and separately "
                       "reanalyze the primary normalized raw output."),
        stopping_rule=stopping_rule,
        seeds=seeds,
        run_limit=2 * len(seeds),
        implementation=compiled["implementation"],
        environment=compiled["environment"],
        data=compiled["data"],
        replication_tolerance=0,
        statistical_design=design,
        seen_data=seen,
    )
    tree = search.register_tree(
        weights={key: 1 for key in COMPONENTS},
        cost_weight=0,
        budget=2 * len(seeds),
        cost_unit="enqueued_attempt",
        max_nodes=1, max_depth=0, max_width=1,
        max_selections=1, max_retries=0,
    )
    node = search.add_node(
        tree, protocol=protocol, action="baseline",
        estimated_cost=2 * len(seeds),
        components=dict(discrimination=0, uncertainty=1, coverage=1, invalidity_risk=0),
        rationale=("Fixed offline re-measurement of one already selected historical run; "
                   "capture fixes the consistency outcome, so discrimination is zero and "
                   "priority is illustrative."),
    )
    correlation = f"afterlife-pilot-{uuid4().hex}"
    execution = {key: compiled[key] for key in (
        "reanalysis_implementation", "reanalysis_environment", "outputs",
        "wall_seconds", "max_output_bytes", "required_capabilities")}
    source_digest = store.put(Path(inspect.getfile(AfterlifeSeedBatchAnalysisAdapter)).read_bytes())
    binding = _command(store, action="domain.bind", study=study, correlation=correlation,
        cause=protocol, payload=dict(
            protocol=protocol,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            adapter_source_digest=source_digest,
            recipe=compiled["recipe"],
            recipe_artifacts=compiled["recipe_artifacts"],
            **execution,
        ))
    selection = search.select_next(tree)
    if selection["node"] != node or selection["protocol"] != protocol:
        raise ValueError("the frozen Afterlife node was not selected")
    batch = _command(store, action="batch.plan", study=study, correlation=correlation,
        cause=selection["id"], payload=dict(
            selection=selection["id"], executor=EXECUTOR, replicator=REANALYST,
            **execution,
        ))
    return dict(study=study, exposure=exposure, question=question,
                explanation_set=explanation_set, protocol=protocol, tree=tree,
                node=node, binding=binding, selection=selection["id"], batch=batch,
                source_run=str(source_run))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    prepare = operations.add_parser("prepare", help="Freeze a new offline pilot; do not execute it")
    prepare.add_argument("--source-run", type=Path, required=True,
                         help="Existing Afterlife run directory containing manifest.json")
    prepare.add_argument("--root", type=Path, required=True,
                         help="New EpistemeOS research directory outside the source checkout")
    args = parser.parse_args()
    source_run = args.source_run.absolute()
    root = args.root.resolve()
    if not source_run.is_dir() or not (source_run / "manifest.json").is_file():
        raise ValueError("source-run must be an Afterlife run directory with manifest.json")
    if root.exists():
        raise ValueError("pilot requires a new research directory")
    # The usual source layout is CHECKOUT/runs/STAGE/RUN. Do not create state
    # anywhere in that checkout, even if it is outside the selected run.
    checkout = (source_run.parents[2] if source_run.parent.parent.name == "runs"
                else source_run).resolve(strict=True)
    if root.is_relative_to(checkout):
        raise ValueError("pilot root must be outside the historical source checkout")
    with Store(root) as store:
        compiled = afterlife_seed.compile_seed_bundle(store, source_run)
        result = _study(store, compiled, source_run)
    batch = result["batch"]
    root_text = str(root)
    result.update(
        status="prepared",
        scientific_validity="not_assessed",
        historical_exposure="declared_before_exploratory_protocol",
        next_steps=[
            ["episteme", "batch", "advance", batch, "--root", root_text],
            ["episteme", "analysis", "advance", batch, "--adapter", ADAPTER_ID,
             "--root", root_text, "--planner", PLANNER, "--analyst", ANALYST,
             "--reviewer", REVIEWER],
            ["episteme", "analysis", "status", batch, "--root", root_text],
        ],
        notice=("These commands use historical bytes and local Python only. "
                "The review remains unsubmitted; role labels are not authenticated independence."),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
