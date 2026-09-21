"""Synthetic Search -> frozen batch -> real local jobs, stopping before analysis."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from episteme.batch_controller import advance_batch
from episteme.commands import CommandService
from episteme.execution import freeze_environment
from episteme.kernel import Actor, Kernel
from episteme.reporting import export_store
from episteme.search import COMPONENTS, Search
from episteme.store import Store
from local_execution import PRIMARY, REANALYSIS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="New research directory")
    args = parser.parse_args()
    if args.root.exists():
        raise ValueError("example requires a new directory")
    with Store(args.root) as store:
        actor = Actor("fixture-planner", "planner")
        kernel, search = Kernel(store, actor), Search(store, actor)
        scope = {"mode": "synthetic_demo", "population": "three synthetic values",
                 "purpose": "scheduler engineering fixture"}
        pool = [kernel.hypothesis(text, prediction, falsifier, scope) for text, prediction, falsifier in (
            ("Positive fixed mean", "Mean > 0", "Mean <= 0"),
            ("Nonpositive fixed mean", "Mean <= 0", "Mean > 0"))]
        code, recode = store.put(PRIMARY), store.put(REANALYSIS)
        environment = freeze_environment(store)
        tree = search.register_tree(weights={key: 1 for key in COMPONENTS}, cost_weight=0,
            budget=4, cost_unit="enqueued_attempt", max_nodes=4, max_depth=2, max_width=2,
            max_selections=2, max_retries=0)
        for index, values in enumerate(([1, 2, 3], [-1, 0, 1])):
            protocol = kernel.preregister(hypotheses=pool, scope=scope,
                design="Compute a fixed synthetic sample mean", metric="mean",
                analysis_plan="sum/len primary, separately coded statistics.mean reanalysis",
                stopping_rule="Primary and same-data reanalysis for seeds 1 and 2, no retry",
                seeds=[1, 2], run_limit=4, implementation=code, environment=environment,
                data=store.put_json({"values": values}), replication_tolerance=0)
            search.add_node(tree, protocol=protocol, action="discriminate", estimated_cost=4,
                components=dict(discrimination=1-index/2, uncertainty=1, coverage=1, invalidity_risk=0),
                rationale="Scripted fixture priority, no scientific ranking claim")
        selection = search.select_next(tree)
        batch = CommandService(store).execute(dict(context=dict(command_id=uuid4().hex,
            expected_revision=len(store.events()), actor=actor.id, role=actor.role,
            study_id="batch-example", correlation_id="batch-example-cycle", causation_id=selection["id"]),
            request=dict(version=1, action="batch.plan", payload=dict(selection=selection["id"],
                executor="fixture-executor", replicator="fixture-reanalyst", reanalysis_implementation=recode,
                reanalysis_environment=environment, outputs={"raw_data": "raw.json", "metrics": "metrics.json"},
                wall_seconds=10, max_output_bytes=65536))))
        state = advance_batch(store, batch)
        print(json.dumps(dict(batch=state, search=search.tree_state(tree), files=export_store(store),
            notice="Synthetic fixture; same-data reanalysis; no independent AI agents, analysis or review."), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
