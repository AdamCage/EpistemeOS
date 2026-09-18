"""Small real local execution/reanalysis example. No AI or scientific approval."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from episteme.commands import CommandService
from episteme.execution import freeze_environment, work_job
from episteme.kernel import Actor, Kernel
from episteme.reporting import export_store
from episteme.store import Store


PRIMARY = b'''import json, pathlib, sys
raw = pathlib.Path(sys.argv[1]).read_bytes()
values = json.loads(raw)["values"]
pathlib.Path("raw.json").write_bytes(raw)
pathlib.Path("metrics.json").write_text(json.dumps({"mean": sum(values)/len(values)}))
print("Primary computation on explicitly synthetic inputs")
'''

REANALYSIS = b'''import json, pathlib, statistics, sys
observations = pathlib.Path(sys.argv[1]).read_bytes()
pathlib.Path("raw.json").write_bytes(observations)
result = statistics.mean(json.loads(observations)["values"])
pathlib.Path("metrics.json").write_text(json.dumps({"mean": result}))
print("Separate implementation on the same observations; not new-data replication")
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="New research directory")
    args = parser.parse_args()
    if args.root.exists():
        raise ValueError("example requires a new directory")
    with Store(args.root) as store:
        planner = Kernel(store, Actor("example-planner", "planner"))
        scope = {"population": "three explicitly synthetic values", "inference": "descriptive example"}
        hypotheses = [planner.hypothesis("Positive sample mean", "Mean is positive", "Mean is nonpositive", scope),
                      planner.hypothesis("Nonpositive sample mean", "Mean is nonpositive", "Mean is positive", scope)]
        code, recode = store.put(PRIMARY), store.put(REANALYSIS)
        environment = freeze_environment(store)
        protocol = planner.preregister(hypotheses=hypotheses, scope=scope, design="Compute a fixed sample mean",
            metric="mean", analysis_plan="Preserve all three values; repeat calculation with statistics.mean",
            stopping_rule="One primary execution and one separate reanalysis; no retries",
            seeds=[1], run_limit=2, implementation=code, environment=environment,
            data=store.put_json({"values": [1, 2, 3]}), replication_tolerance=0)

        def enqueue(actor, **options):
            envelope = dict(context=dict(command_id=uuid4().hex, expected_revision=len(store.events()),
                actor=actor.id, role=actor.role, study_id="local-execution-example", correlation_id="example-cycle",
                causation_id=protocol), request=dict(version=1, action="execution.enqueue", payload=dict(
                protocol=protocol, seed=1, outputs={"raw_data": "raw.json", "metrics": "metrics.json"},
                wall_seconds=10, max_output_bytes=65536, **options)))
            return CommandService(store).execute(envelope)

        job = enqueue(Actor("example-executor", "executor"))
        primary = work_job(store, job)
        if primary["status"] != "completed":
            print(json.dumps(primary, indent=2))
            return 1
        replica = enqueue(Actor("example-reanalyst", "replicator"), implementation=recode, replicate_of=primary["run"])
        repeated = work_job(store, replica)
        if repeated["status"] != "completed":
            print(json.dumps(repeated, indent=2))
            return 1
        analyst = Kernel(store, Actor("example-analyst", "analyst"))
        claim = analyst.claim(protocol=protocol, statement="The fixed synthetic sample has mean 2.", scope=scope,
            evidence=[primary["run"], repeated["run"]], outcome="inconclusive", limitations=[
                "Engineering example, no scientific discovery or independent AI agents.",
                "Separate code uses the same observations; this is not new-data replication.",
                "Trusted local execution; no filesystem/network sandbox or portable environment closure."])
        print(json.dumps(dict(primary=primary, reanalysis=repeated, claim=claim, gate=analyst.gate(claim),
                              next_action=analyst.next_action(claim), files=export_store(store)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
