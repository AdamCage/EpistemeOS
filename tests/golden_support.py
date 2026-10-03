"""Load saved fixture histories and observe their derived hashes.

The saved histories are synthetic fixtures produced by the code at the commit
recorded in ``tests/fixtures/golden/expected.json``. Observations recompute
review bases, gates, Graph, export bundle and replay projections with the code
under test; equality means those recipes did not change for old histories. It
says nothing about scientific validity.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import re
from typing import Any

from episteme.analysis_controller import analysis_state
from episteme.batch import _index as batch_index, batch_state
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.reporting import artifact_inventory, review_bundle
from episteme.store import Store, canonical, digest


GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden"
FORMAT = "episteme-golden-history-v1"


def export_history(store: Store) -> dict[str, Any]:
    receipts = [{key: value for key, value in receipt.items() if key != "hash"}
                for receipt in store.receipts()]
    artifacts = {path.name: base64.b64encode(path.read_bytes()).decode("ascii")
                 for path in sorted(store.blobs.iterdir())
                 if re.fullmatch(r"[0-9a-f]{64}", path.name)}
    return dict(format=FORMAT, events=store.events(), receipts=receipts, artifacts=artifacts)


def load_history(fixture: dict[str, Any], root: Path) -> Store:
    """Install exact saved rows and blobs; Store verification runs on every read."""
    if fixture.get("format") != FORMAT:
        raise ValueError("unsupported golden history format")
    store = Store(root)
    try:
        for key, encoded in fixture["artifacts"].items():
            data = base64.b64decode(encoded, validate=True)
            if digest(data) != key:
                raise ValueError(f"golden artifact digest mismatch: {key}")
            (store.blobs / key).write_bytes(data)
        with store.db:
            for event in fixture["events"]:
                store.db.execute(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event["seq"], event["id"], event["kind"], event["actor"], event["role"],
                     event["created_at"], event["schema_version"],
                     canonical(event["payload"]).decode(), event["previous_hash"],
                     event["hash"]))
            for receipt in fixture["receipts"]:
                data = canonical(receipt)
                store.db.execute("INSERT INTO command_receipts VALUES (?, ?, ?)",
                                 (receipt["command_id"], data.decode(), digest(data)))
        if store.events() != fixture["events"]:
            raise ValueError("golden history did not round-trip")
        return store
    except BaseException:
        store.close()
        raise


def _sha(value: Any) -> str:
    return digest(canonical(value))


def observe(store: Store) -> dict[str, Any]:
    """Recompute every derived projection that old histories must preserve."""
    from episteme.domains.synthetic_batch_analysis import SyntheticCausalBatchAnalysisAdapter

    history = store.events()
    kernel = Kernel(store, Actor("golden-observer", "observer"))
    claims = {}
    for event in history:
        if event["kind"] != "claim":
            continue
        gate = kernel._gate(history, event["id"])
        claims[event["id"]] = dict(basis_hash=gate["basis_hash"], gate_sha256=_sha(gate),
                                   passed=gate["passed"],
                                   next_action=kernel._next_action(history, event["id"]))
    graph = ResearchGraph.from_store(store)
    bundle = review_bundle(store, history)
    bundle["summary"].pop("root")
    batches = {}
    states = batch_index(store, history)
    for id in states:
        batches[id] = dict(batch_state_sha256=_sha(batch_state(store, id)),
                           analysis_state_sha256=_sha(analysis_state(store, id)))
    analyses = {}
    for event in history:
        if event["kind"] != "batch_analysis":
            continue
        p = event["payload"]
        row = dict(adapter_id=p["adapter_id"], proposal_digest=p["proposal_digest"])
        if p["adapter_id"] == SyntheticCausalBatchAnalysisAdapter.adapter_id:
            proposal = SyntheticCausalBatchAnalysisAdapter.propose(store, states[p["batch"]])
            row["reproposal_digest"] = _sha(proposal)
        analyses[event["id"]] = row
    return dict(revision=len(history), head=history[-1]["hash"] if history else "0" * 64,
                receipts_sha256=_sha(store.receipts()), claims=claims,
                graph=dict(sha256=digest(graph.to_json().encode("utf-8")),
                           nodes=len(graph.nodes), edges=len(graph.edges)),
                artifact_inventory_sha256=_sha(artifact_inventory(store, history)),
                review_bundle_sha256=_sha(bundle), batches=batches, analyses=analyses)


def legacy_synthetic_recipes() -> dict[str, Any]:
    """Frozen outputs of the legacy synthetic compiler on a fixed parameter grid."""
    from episteme.domains import synthetic_batch_analysis, synthetic_causal

    world = dict(treatment_effect=2.0, confounding_strength=0.5, noise_std=1.0)
    compiled = {}
    for count in (32, 512):
        for assignment in ("randomized", "observational"):
            for analysis in ("difference_in_means", "adjusted_ols"):
                result = synthetic_causal.compile_recipe(
                    dict(n_samples=count, assignment=assignment, analysis=analysis), world)
                encoded = {key: (dict(sha256=digest(value)) if type(value) is bytes else value)
                           for key, value in result.items()}
                compiled[f"{count}:{assignment}:{analysis}"] = _sha(encoded)
    return dict(
        synthetic_causal_sha256=digest(Path(synthetic_causal.__file__).read_bytes()),
        synthetic_batch_analysis_sha256=digest(
            Path(synthetic_batch_analysis.__file__).read_bytes()),
        describe_sha256=_sha(synthetic_causal.describe()), compiled=compiled)


def read_json(name: str) -> Any:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))
