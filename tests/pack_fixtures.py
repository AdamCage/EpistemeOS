"""Conformance inputs for every DomainPack and a minimal local runner.

Each registered pack must have an entry in ``FIXTURES``; a pack without one
fails the conformance suite. The runner below executes the compiled programs
the way ``trusted_local_python_v1`` does (``python -I -S program.py input.dat
--seed N`` in a fresh directory) without a Store; it is test scaffolding, not
the kernel's execution path, and its synthetic outputs are not evidence.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from episteme.domains import api
from episteme.store import digest


PACKS = Path(__file__).resolve().parent / "fixtures" / "packs"
if str(PACKS) not in sys.path:
    sys.path.insert(0, str(PACKS))

FIXTURE_PACK = "conformance_fixture_v1"


def _fixture_request(workdir: Path) -> api.CompileRequest:
    return api.CompileRequest(parameters={"scale": 2, "outcome": "inconclusive",
                                          "inference_mode": "descriptive"},
                              host_inputs={"groups": [[1, 2, 3], [4, 5], [-7]]}, capture=None)


def _synthetic_request(workdir: Path) -> api.CompileRequest:
    return api.CompileRequest(
        parameters={"n_samples": 48, "assignment": "observational", "analysis": "adjusted_ols"},
        host_inputs={"world": {"treatment_effect": 1.5, "confounding_strength": 0.7,
                               "noise_std": 0.5},
                     "seeds": [3, 5], "replication_tolerance": 1e-9},
        capture=None)


def historical_run(workdir: Path) -> Path:
    """The 30-output synthetic layout of one historical Afterlife run (test fixture)."""
    import test_afterlife_seed_batch
    builder = test_afterlife_seed_batch.AfterlifeSeedBatchTests(
        methodName="test_complete_roster_is_frozen_byte_for_byte_without_touching_source")
    builder.source = workdir / "historical-run"
    if builder.source.exists():
        return builder.source  # The layout is deterministic; build it once per workdir.
    builder.source.mkdir(parents=True)
    builder.manifest = builder._build_historical_fixture()
    return builder.source


def _afterlife_request(workdir: Path) -> api.CompileRequest:
    from episteme.domains import registry
    capture = registry.load_pack("afterlife_seed_v1").hook("capture")(historical_run(workdir))
    return api.CompileRequest(parameters={}, host_inputs={}, capture=capture)


def _tabular_request(workdir: Path) -> api.CompileRequest:
    from episteme.domains import registry
    source = workdir / "source"
    source.mkdir(parents=True, exist_ok=True)
    planted = Path(__file__).resolve().parents[1] / "examples" / "tabular_classification_v1" / "planted"
    for name in ("train.csv", "holdout.csv"):
        (source / name).write_bytes((planted / name).read_bytes())
    capture = registry.load_pack("tabular_classification_v1").hook("capture")(source)
    return api.CompileRequest(parameters={}, host_inputs={}, capture=capture)


# pack_id -> (module name for registration, compile-request builder)
FIXTURES: dict[str, tuple[str, Callable[[Path], api.CompileRequest]]] = {
    FIXTURE_PACK: (FIXTURE_PACK, _fixture_request),
    "synthetic_causal_v1": ("episteme.domains.packs.synthetic_causal_v1", _synthetic_request),
    "afterlife_seed_v1": ("episteme.domains.packs.afterlife_seed_v1", _afterlife_request),
    "tabular_classification_v1": ("episteme.domains.packs.tabular_classification_v1", _tabular_request),
}


def _run(program: bytes, data: bytes, unit: int, workdir: Path,
         outputs: dict[str, str]) -> dict[str, bytes]:
    workdir.mkdir(parents=True)
    (workdir / "program.py").write_bytes(program)
    (workdir / "input.dat").write_bytes(data)
    subprocess.run([sys.executable, "-I", "-S", "program.py", "input.dat", "--seed", str(unit)],
                   cwd=workdir, check=True, capture_output=True, timeout=120)
    return {label: (workdir / path).read_bytes() for label, path in outputs.items()}


def run_programs(plan: api.ExecutionPlan, roster: Any, workdir: Path
                 ) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Primary then same-data reanalysis for each roster unit, like a batch."""
    outputs = dict(plan.outputs)
    slots: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {}
    for unit in roster:
        primary = _run(plan.primary_program, plan.input, unit, workdir / f"primary-{unit}", outputs)
        repeated = _run(plan.reanalysis_program, primary["raw_data"], unit,
                        workdir / f"reanalysis-{unit}", outputs)
        for label, mode, produced in (("primary", "primary", primary),
                                      ("reanalysis", "independent_reanalysis", repeated)):
            keys = {}
            for output, data in produced.items():
                keys[output] = digest(data)
                blobs[keys[output]] = data
            slots.append(dict(slot=f"{label}:{unit}", roster_unit=unit, mode=mode,
                              run=f"run-{label}-{unit}", result=f"result-{label}-{unit}",
                              outputs=keys))
    return slots, blobs


def protocol_payload(draft: api.ProtocolDraft, plan: api.ExecutionPlan) -> dict[str, Any]:
    """A protocol payload as the kernel would record it, without a Store."""
    frozen = plan.to_dict()
    design = api.thaw(draft.statistical_design)
    return dict(hypotheses=["hypothesis-a", "hypothesis-b"], scope={"fixture": "conformance"},
                design=draft.design, metric=draft.metric, analysis_plan=draft.analysis_plan,
                stopping_rule=draft.stopping_rule, seeds=list(draft.roster),
                run_limit=draft.run_limit, implementation=frozen["primary_program"]["sha256"],
                environment="0" * 64, data=frozen["input"]["sha256"],
                replication_tolerance=draft.replication_tolerance, parent=None,
                statistical_design=design, protocol_mode=design["mode"], amendment_reason=None,
                seen_data=list(draft.seen_data))


def analysis_inputs(manifest: api.PackManifest, request: api.CompileRequest,
                    draft: api.ProtocolDraft, plan: api.ExecutionPlan,
                    slots: list[dict[str, Any]], blobs: dict[str, bytes],
                    protocol_hash: str = "f" * 64) -> tuple[api.AnalysisContext, api.CasView]:
    protocol = protocol_payload(draft, plan)
    context = api.AnalysisContext(
        pack_id=manifest.pack_id, pack_version=manifest.pack_version, protocol=protocol,
        protocol_hash=protocol_hash, parameters=request.parameters, draft=draft,
        execution_plan=plan.to_dict(),
        capture=None if request.capture is None else request.capture.to_dict(),
        batch=dict(plan="batch-fixture", settlement="settlement-fixture"), slots=slots,
        numeric_tolerance=manifest.numeric_tolerance)
    allowed = {key: data for row in slots for label, key in row["outputs"].items()
               if label in manifest.outputs for data in (blobs[key],)}
    hidden: set[str] = set()
    data_key = protocol["data"]
    if "protocol_data" in manifest.hidden_inputs:
        hidden.add(data_key)
    else:
        allowed[data_key] = plan.input
    if request.capture is not None:
        captured = request.capture.blobs()
        if "captured_artifacts" in manifest.hidden_inputs:
            hidden.update(captured)
        else:
            allowed.update(captured)
    for key in hidden:
        allowed.pop(key, None)
    return context, api.CasView(allowed, hidden=hidden - set(allowed))
