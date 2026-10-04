"""Command-line interface for the local research kernel."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Iterator, Sequence

from .commands import CommandService, parse_command
from .demo import run_demo
from .domains.afterlife import import_snapshot, inspect as inspect_afterlife
from .graph import ResearchGraph
from .kernel import Actor, Kernel
from .reporting import PaperBuilder, export_store, inspect_store
from .recovery import backup, restore
from .store import Store
from .execution import freeze_environment, job_state, reconcile_job, work_job
from . import execution_locked
from .execution_locked import OPERATIONS as LOCKED_OPERATIONS, add_arguments as add_locked_arguments
from .execution_locked import run_cli as run_locked_cli


def _actor_id(value: str) -> str:
    """Caller-declared, unauthenticated actor ID in its canonical form (ADR 0018)."""
    from .kernel import canonical_actor
    if not canonical_actor(value):
        raise argparse.ArgumentTypeError(
            "actor IDs must be 1-128 lowercase ASCII letters, digits or ._@:- "
            "that start and end with a letter or digit")
    return value


@contextmanager
def _opened(root: Path, *, read_only: bool = False) -> Iterator[Store]:
    """One CLI command is one CAS read scope; write transactions reread their artifacts."""
    with Store(root, read_only=read_only) as store, store.reading():
        yield store


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="episteme", description="Local evidence-first research kernel")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("demo", "Execute a new offline synthetic CPU fixture"),
                            ("inspect", "Verify and inspect existing state"),
                            ("gate", "Check a claim's mechanical evidence gates"),
                            ("export", "Export a review bundle and report from existing state"),
                            ("review", "Record an externally prepared scientific review JSON"),
                            ("paper", "Build an internal draft from currently reviewed claims"),
                            ("command", "Admit or replay a versioned local command JSON"),
                            ("receipts", "Verify and read historical command acknowledgements"),
                            ("backup", "Create a verified SQLite and artifact directory snapshot"),
                            ("graph", "Verify recorded dependencies and export the research graph")):
        command = subcommands.add_parser(name, help=help_text)
        if name == "demo":
            command.add_argument("--with-search", action="store_true",
                                 help="Include a scripted preference tournament and bounded experiment frontier")
        if name in {"gate", "review"}:
            command.add_argument("claim", help="Recorded claim ID")
        if name == "review":
            command.add_argument("--input", type=Path, required=True, help="Review JSON file")
        if name == "command":
            command.add_argument("--input", type=Path, required=True, help="Versioned command envelope JSON")
        if name == "backup":
            command.add_argument("--output", type=Path, required=True, help="New snapshot directory")
        if name == "paper":
            command.add_argument("claims", nargs="+", help="Reviewed claim IDs")
            command.add_argument("--title", required=True)
            command.add_argument("--actor", required=True, type=_actor_id, help="Trusted caller's writer ID")
        if name == "graph":
            command.add_argument("--format", choices=("summary", "json", "dot"), default="summary")
        command.add_argument("--root", type=Path, required=True, help="Research state directory")
    recovery = subcommands.add_parser("restore", help="Verify and restore a snapshot to a new state directory")
    recovery.add_argument("snapshot", type=Path)
    recovery.add_argument("--root", type=Path, required=True, help="New research state directory")
    afterlife = subcommands.add_parser("afterlife", help="Observe or import read-only historical Afterlife records")
    operations = afterlife.add_subparsers(dest="operation", required=True)
    for name in ("inspect", "import"):
        operation = operations.add_parser(name)
        operation.add_argument("source", type=Path, help="Existing Afterlife checkout")
        operation.add_argument("--max-verify-mib", type=int, default=256,
                               help="Bound total streamed output-hash verification; skipped outputs remain unverified")
        if name == "import":
            operation.add_argument("--root", type=Path, required=True)
            operation.add_argument("--actor", default="afterlife-importer", type=_actor_id)
    execution = subcommands.add_parser("execution", help="Explicit trusted local Python execution; no sandbox")
    execution_ops = execution.add_subparsers(dest="operation", required=True)
    for name in ("environment", "status", "work", "reconcile"):
        operation = execution_ops.add_parser(name)
        if name != "environment":
            operation.add_argument("job", help="Recorded execution_job ID")
        operation.add_argument("--root", type=Path, required=True)
    execution_locked.add_arguments(execution_ops)
    batches = subcommands.add_parser("batch", help="Resume a frozen local attempt roster; no scientific approval")
    batch_ops = batches.add_subparsers(dest="operation", required=True)
    for name in ("status", "advance"):
        operation = batch_ops.add_parser(name)
        operation.add_argument("batch", help="Recorded batch_plan ID")
        operation.add_argument("--root", type=Path, required=True)
    analyses = subcommands.add_parser("analysis", help="Admit a batch claim and assign review; no verdict")
    analysis_ops = analyses.add_subparsers(dest="operation", required=True)
    for name in ("status", "advance"):
        operation = analysis_ops.add_parser(name)
        operation.add_argument("batch", help="Completed batch_plan ID")
        operation.add_argument("--root", type=Path, required=True)
        if name == "advance":
            operation.add_argument("--adapter", default=None,
                                   help="Optional assertion; must equal the pack or adapter the "
                                        "protocol binding fixes")
            operation.add_argument("--planner", required=True, type=_actor_id, help="Caller-declared batch planner ID")
            operation.add_argument("--analyst", required=True, type=_actor_id, help="Caller-declared analyst ID")
            operation.add_argument("--reviewer", required=True, type=_actor_id, help="Caller-declared reviewer ID")
    analysis_verify = analysis_ops.add_parser(
        "verify", help="Recompute every admitted batch analysis with its registered adapter; read-only")
    analysis_verify.add_argument("--root", type=Path, required=True)
    packs = subcommands.add_parser("pack", help="Describe or verify pinned DomainPacks; read-only")
    pack_ops = packs.add_subparsers(dest="operation", required=True)
    pack_describe = pack_ops.add_parser("describe", help="Live registered pack identity and catalog")
    pack_describe.add_argument("pack_id")
    pack_verify = pack_ops.add_parser("verify", help="Re-run pinned hooks; compare recorded bytes")
    pack_verify.add_argument("--root", type=Path, required=True)
    pack_capture = pack_ops.add_parser("capture", help="Read a declared source once into CAS; no events")
    pack_capture.add_argument("pack_id")
    pack_capture.add_argument("--source", type=Path, required=True, help="Existing local source directory")
    pack_capture.add_argument("--root", type=Path, required=True, help="Research state outside the source")
    followups = subcommands.add_parser("followup", help="Inspect an open review obligation and its child plan")
    followup_ops = followups.add_subparsers(dest="operation", required=True)
    followup_status = followup_ops.add_parser("status")
    followup_status.add_argument("obligation", help="Recorded review_obligation ID")
    followup_status.add_argument("--root", type=Path, required=True)
    agents = subcommands.add_parser("agent", help="Explicit bounded Codex proposals; no scientific approval")
    agent_ops = agents.add_subparsers(dest="operation", required=True)
    for name in ("provider", "recipe", "status", "work", "reconcile", "advance"):
        operation = agent_ops.add_parser(name)
        if name == "provider":
            operation.add_argument("--model", required=True)
            operation.add_argument("--reasoning-effort", default="low")
            operation.add_argument("--executable", type=Path)
        elif name == "recipe":
            operation.add_argument("--input", type=Path, required=True,
                                   help="Host-owned synthetic world, seeds and environment JSON")
        else:
            operation.add_argument("request", help="Recorded agent_request ID")
        operation.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "followup":
            from .followup import followup_state
            if not (args.root / "state.sqlite3").is_file():
                raise ValueError("existing research state is required")
            with _opened(args.root, read_only=True) as store:
                result = followup_state(store, args.obligation)
            status = 0
        elif args.command == "agent":
            from .agents import agent_state, freeze_recipe_binding
            from .agent_controller import work_agent, reconcile_agent, advance_agent
            from .codex_provider import freeze_provider
            if args.operation not in {"provider", "recipe"} and not (args.root / "state.sqlite3").is_file():
                raise ValueError("existing research state is required")
            with _opened(args.root, read_only=args.operation == "status") as store:
                if args.operation == "provider":
                    result = dict(provider=freeze_provider(store, model=args.model,
                        reasoning_effort=args.reasoning_effort, executable=args.executable),
                        meaning="frozen local CLI descriptor; no model call")
                elif args.operation == "recipe":
                    descriptor = parse_command(args.input.read_text(encoding="utf-8"))
                    if type(descriptor) is not dict or set(descriptor) != {
                            "world", "seeds", "environment", "replication_tolerance"}:
                        raise ValueError("recipe input requires world, seeds, environment, replication_tolerance")
                    result = dict(recipe_binding=freeze_recipe_binding(store, **descriptor),
                                  meaning="frozen host recipe; no model call or experiment")
                else:
                    action = {"status": agent_state, "work": work_agent,
                              "reconcile": reconcile_agent, "advance": advance_agent}[args.operation]
                    result = action(store, args.request)
            status = 0
        elif args.command == "analysis":
            from .analysis_controller import (advance_batch_analysis, advance_pack_analysis,
                                              analysis_state, bound_analysis)
            from .domains.registry import legacy_analysis_adapter
            if not (args.root / "state.sqlite3").is_file():
                raise ValueError("existing research state is required")
            with _opened(args.root, read_only=args.operation in {"status", "verify"}) as store:
                if args.operation == "verify":
                    from .batch_analysis import verify
                    result = verify(store)
                elif args.operation == "status":
                    result = analysis_state(store, args.batch)
                else:
                    # The protocol binding fixes the analysis code; --adapter only asserts it.
                    bound = bound_analysis(store, args.batch)
                    if args.adapter is not None and args.adapter != bound["id"]:
                        raise ValueError(f"--adapter {args.adapter} differs from the bound "
                                         f"{bound['kind']} {bound['id']}")
                    actors = dict(planner=Actor(args.planner, "planner"),
                                  analyst=Actor(args.analyst, "analyst"), reviewer_actor=args.reviewer)
                    if bound["kind"] == "pack":
                        result = advance_pack_analysis(store, args.batch, **actors)
                    else:
                        adapter, _ = legacy_analysis_adapter(bound["id"])
                        result = advance_batch_analysis(store, args.batch, adapter=adapter, **actors)
            status = 1 if args.operation == "verify" and result["status"] != "matched" else 0
        elif args.command == "pack":
            from .domain_packs import describe_pack, store_capture, verify
            if args.operation == "describe":
                result, status = describe_pack(args.pack_id), 0
            elif args.operation == "capture":
                source, root = args.source.resolve(), args.root.resolve()
                if root.is_relative_to(source) or source.is_relative_to(root):
                    raise ValueError("capture source and research state must not overlap")
                with _opened(args.root) as store:
                    result, status = store_capture(store, args.pack_id, args.source), 0
            else:
                if not (args.root / "state.sqlite3").is_file():
                    raise ValueError("existing research state is required")
                with _opened(args.root, read_only=True) as store:
                    result = verify(store)
                status = 0 if result["status"] == "matched" else 1
        elif args.command == "batch":
            from .batch import batch_state
            from .batch_controller import advance_batch
            if not (args.root / "state.sqlite3").is_file():
                raise ValueError("existing research state is required")
            with _opened(args.root, read_only=args.operation == "status") as store:
                result = (batch_state if args.operation == "status" else advance_batch)(store, args.batch)
            status = 0
        elif args.command == "execution" and args.operation in execution_locked.OPERATIONS:
            result, status = execution_locked.run_cli(args)
        elif args.command == "execution":
            if args.operation != "environment" and not (args.root / "state.sqlite3").is_file():
                raise ValueError("existing research state is required")
            with _opened(args.root, read_only=args.operation == "status") as store:
                if args.operation == "environment":
                    result = dict(environment=freeze_environment(store), isolation="trusted local; no sandbox")
                else:
                    action = {"status": job_state, "work": work_job, "reconcile": reconcile_job}[args.operation]
                    result = action(store, args.job)
            status = 0
        elif args.command == "afterlife":
            if args.operation == "import" and args.root.resolve().is_relative_to(args.source.resolve()):
                raise ValueError("import destination must be outside the read-only source checkout")
            snapshot = inspect_afterlife(args.source, max_verify_total_bytes=args.max_verify_mib * 1024 * 1024)
            if args.operation == "inspect":
                result = snapshot.report()
            else:
                with _opened(args.root) as store:
                    result = import_snapshot(store, snapshot, actor=args.actor)
            status = 0
        elif args.command == "demo":
            result = run_demo(args.root, with_search=args.with_search)
            status = 0
        elif args.command == "restore":
            result, status = restore(args.snapshot, args.root), 0
        elif args.command == "command":
            envelope = parse_command(args.input.read_text(encoding="utf-8"))
            with _opened(args.root) as store:
                acknowledgement = CommandService(store).execute(envelope)
                result = dict(command_id=envelope["context"]["command_id"], result=acknowledgement,
                              meaning="historical_command_commit")
            status = 0
        else:
            if not (args.root / "state.sqlite3").is_file():
                raise ValueError("existing state.sqlite3 is required; inspect/export/gate never initialize a project")
            with _opened(args.root, read_only=args.command not in {"review", "paper"}) as store:
                if args.command == "inspect":
                    result, status = inspect_store(store), 0
                elif args.command == "backup":
                    result, status = backup(store, args.output), 0
                elif args.command == "receipts":
                    result, status = dict(receipts=store.receipts(),
                                          meaning="historical_command_commits"), 0
                elif args.command == "export":
                    result, status = export_store(store), 0
                elif args.command == "graph":
                    graph = ResearchGraph.from_store(store)
                    if args.format == "dot":
                        print(graph.to_dot(), end="")
                        return 0
                    result = (graph.to_dict() if args.format == "json" else {
                        "revision": graph.revision, "snapshot_hash": graph.snapshot_hash,
                        "nodes": len(graph.nodes), "edges": len(graph.edges),
                        "node_kinds": dict(Counter(node.kind.value for node in graph.nodes)),
                        "scientific_validity": "not_assessed"})
                    status = 0
                elif args.command == "review":
                    review = json.loads(args.input.read_text(encoding="utf-8"))
                    fields = {"reviewer_id", "verdict", "rationale", "actions", "expected_basis"}
                    if not isinstance(review, dict) or set(review) != fields:
                        raise ValueError("review JSON fields must be exactly: " + ", ".join(sorted(fields)))
                    if not isinstance(review["reviewer_id"], str) or not review["reviewer_id"].strip():
                        raise ValueError("reviewer_id must be a nonempty string")
                    reviewer = Kernel(store, Actor(review.pop("reviewer_id"), "reviewer"))
                    id = reviewer.review(args.claim, **review)
                    result, status = {"review": id, "next_action": reviewer.next_action(args.claim)}, 0
                elif args.command == "paper":
                    writer = Kernel(store, Actor(args.actor, "writer"))
                    bases = {id: writer.gate(id)["basis_hash"] for id in args.claims}
                    builder = PaperBuilder(store, writer.actor)
                    id = builder.build(title=args.title, claims=args.claims, expected_bases=bases)
                    result, status = {"paper": id, "status": "internal_draft",
                                      "files": builder.materialize(id)}, 0
                else:
                    result = Kernel(store, Actor("cli-inspector", "observer")).gate(args.claim)
                    status = 0 if result["passed"] else 1
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return status
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1 if args.command == "gate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
