"""Command-line interface for the local research kernel."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Sequence

from .demo import run_demo
from .domains.afterlife import import_snapshot, inspect as inspect_afterlife
from .graph import ResearchGraph
from .kernel import Actor, Kernel
from .reporting import PaperBuilder, export_store, inspect_store
from .store import Store


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="episteme", description="Local evidence-first research kernel")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("demo", "Execute a new offline synthetic CPU fixture"),
                            ("inspect", "Verify and inspect existing state"),
                            ("gate", "Check a claim's mechanical evidence gates"),
                            ("export", "Export a review bundle and report from existing state"),
                            ("review", "Record an externally prepared scientific review JSON"),
                            ("paper", "Build an internal draft from currently reviewed claims"),
                            ("graph", "Verify recorded dependencies and export the research graph")):
        command = subcommands.add_parser(name, help=help_text)
        if name == "demo":
            command.add_argument("--with-search", action="store_true",
                                 help="Include a scripted preference tournament and bounded experiment frontier")
        if name in {"gate", "review"}:
            command.add_argument("claim", help="Recorded claim ID")
        if name == "review":
            command.add_argument("--input", type=Path, required=True, help="Review JSON file")
        if name == "paper":
            command.add_argument("claims", nargs="+", help="Reviewed claim IDs")
            command.add_argument("--title", required=True)
            command.add_argument("--actor", required=True, help="Trusted caller's writer ID")
        if name == "graph":
            command.add_argument("--format", choices=("summary", "json", "dot"), default="summary")
        command.add_argument("--root", type=Path, required=True, help="Research state directory")
    afterlife = subcommands.add_parser("afterlife", help="Observe or import read-only historical Afterlife records")
    operations = afterlife.add_subparsers(dest="operation", required=True)
    for name in ("inspect", "import"):
        operation = operations.add_parser(name)
        operation.add_argument("source", type=Path, help="Existing Afterlife checkout")
        operation.add_argument("--max-verify-mib", type=int, default=256,
                               help="Bound total streamed output-hash verification; skipped outputs remain unverified")
        if name == "import":
            operation.add_argument("--root", type=Path, required=True)
            operation.add_argument("--actor", default="afterlife-importer")
    args = parser.parse_args(argv)
    try:
        if args.command == "afterlife":
            if args.operation == "import" and args.root.resolve().is_relative_to(args.source.resolve()):
                raise ValueError("import destination must be outside the read-only source checkout")
            snapshot = inspect_afterlife(args.source, max_verify_total_bytes=args.max_verify_mib * 1024 * 1024)
            if args.operation == "inspect":
                result = snapshot.report()
            else:
                with Store(args.root) as store:
                    result = import_snapshot(store, snapshot, actor=args.actor)
            status = 0
        elif args.command == "demo":
            result = run_demo(args.root, with_search=args.with_search)
            status = 0
        else:
            if not (args.root / "state.sqlite3").is_file():
                raise ValueError("existing state.sqlite3 is required; inspect/export/gate never initialize a project")
            with Store(args.root, read_only=args.command not in {"review", "paper"}) as store:
                if args.command == "inspect":
                    result, status = inspect_store(store), 0
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
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1 if args.command == "gate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
