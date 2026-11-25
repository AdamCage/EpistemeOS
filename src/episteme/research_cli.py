"""Local CLI for research sessions and their separate release channel."""

from __future__ import annotations

import argparse
from pathlib import Path
import time
from typing import Any

from .execution import _object
from .kernel import require
from .store import ConflictError, Store


def add_research_command(subcommands: Any) -> None:
    parser = subcommands.add_parser("research", help="Discuss and run a bounded research session")
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("start", "discuss", "run", "status", "export", "worker"):
        operation = operations.add_parser(name)
        operation.add_argument("--root", type=Path, required=True)
        if name not in {"start", "worker"}:
            operation.add_argument("session")
        if name == "start":
            operation.add_argument("--hypothesis", type=Path, required=True)
            operation.add_argument("--config", type=Path, required=True)
            operation.add_argument("--author", default="author")
            operation.add_argument("--release-root", type=Path)
        elif name == "discuss":
            operation.add_argument("--reply", type=Path, required=True)
            operation.add_argument("--author")
        elif name == "run":
            operation.add_argument("--accept", metavar="DRAFT_REVISION")
            operation.add_argument("--author")
        elif name == "export":
            operation.add_argument("--out", type=Path, required=True)
        elif name == "worker":
            operation.add_argument("--once", action="store_true")
            operation.add_argument("--poll-seconds", type=int, default=5)


def _file(path: Path, limit: int) -> bytes:
    require(path.is_file() and path.stat().st_size <= limit, f"missing or oversized input file: {path}")
    with path.open("rb") as file:
        content = file.read(limit + 1)
    require(len(content) <= limit, f"oversized input file: {path}")
    return content


def run_research_cli(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from .kernel import Kernel
    from .research import session_view
    from .research_controller import discuss_research, research_status, run_research, start_research

    name = args.operation
    if name != "start":
        require((args.root / "state.sqlite3").is_file(), "existing research state is required")
    with Store(args.root, read_only=name in {"status", "export"}) as store, store.reading():
        if name == "start":
            return start_research(store, _file(args.hypothesis, 32768).decode("utf-8"),
                                  _object(_file(args.config, 256 * 1024)), author=args.author,
                                  release_root=args.release_root, base_dir=args.config.absolute().parent), 0
        if name == "worker":
            require(1 <= args.poll_seconds <= 60, "worker poll interval must be 1 to 60 seconds")
            while True:
                history = store.events()
                for row in history:
                    if row["kind"] != "research_session":
                        continue
                    steps = session_view(history, row["id"])["steps"]
                    if "accept" in steps and "finish" not in steps:
                        try:
                            run_research(store, row["id"])
                        except ConflictError:
                            continue
                if args.once:
                    return dict(status="idle"), 0
                time.sleep(args.poll_seconds)
        if name == "status":
            return research_status(store, args.session), 0
        if name == "export":
            from .research_export import export_research

            return export_research(store, args.session, args.out), 0
        author = args.author or Kernel._get(store.events(), args.session, "research_session")["actor"]
        if name == "discuss":
            return discuss_research(store, args.session, _file(args.reply, 32768).decode("utf-8"), author=author), 0
        return run_research(store, args.session, accept=args.accept, author=author), 0


def add_evolution_command(subcommands: Any) -> None:
    parser = subcommands.add_parser("evolution", help="Evaluate and activate local research releases")
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("init", "status", "propose", "configure", "evaluate", "health", "advance", "worker", "rollback", "signal"):
        operation = operations.add_parser(name)
        operation.add_argument("--root", type=Path, required=True)
        if name == "init":
            operation.add_argument("--profile", type=Path)
            operation.add_argument("--base-commit")
        elif name == "propose":
            operation.add_argument("--profile", type=Path)
            operation.add_argument("--worktree", type=Path)
            operation.add_argument("--provenance", required=True)
        elif name == "configure":
            operation.add_argument("--config", type=Path, required=True)
            operation.add_argument("--image", required=True)
        elif name in {"evaluate", "health"}:
            operation.add_argument("release")
            operation.add_argument("--suite", type=Path, required=True)
            operation.add_argument("--image", required=True)
        elif name == "advance":
            operation.add_argument("candidate", nargs="?")
        elif name == "worker":
            operation.add_argument("--once", action="store_true")
            operation.add_argument("--poll-seconds", type=int, default=15)
        elif name == "rollback":
            operation.add_argument("release")
            operation.add_argument("--reason", required=True)
        elif name == "signal":
            operation.add_argument("--kind", required=True)
            operation.add_argument("--key", required=True)
            operation.add_argument("--source", required=True)


def run_evolution_cli(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from .evolution import DockerBackend, EvolutionChannel

    name = args.operation
    if name != "init":
        require((args.root / "state.sqlite3").is_file(), "existing release channel is required")
    with EvolutionChannel(args.root, read_only=name == "status") as channel:
        if name == "init":
            profile = _object(_file(args.profile, 65536)) if args.profile else {"role_instructions": {}}
            return dict(release=channel.initialize(profile, base_commit=args.base_commit)), 0
        if name == "status":
            return channel.status(), 0
        if name == "configure":
            config = _object(_file(args.config, 256 * 1024))
            required = {"options", "training", "validation", "suite"}
            require(required <= set(config) <= required | {"max_evaluations", "seed"}, "invalid evolution configuration")
            return dict(configuration=channel.configure(backend=DockerBackend(args.image), **config)), 0
        if name == "propose":
            require((args.profile is None) != (args.worktree is None), "supply a profile or a candidate worktree")
            candidate = (channel.propose_worktree(args.worktree, provenance=args.provenance) if args.worktree else
                         channel.propose(profile=_object(_file(args.profile, 65536)), provenance=args.provenance))
            return dict(candidate=candidate), 0
        if name in {"evaluate", "health"}:
            suite = _object(_file(args.suite, 256 * 1024))
            backend = DockerBackend(args.image)
            if name == "health":
                return channel.health_check(args.release, suite=suite, backend=backend), 0
            paired = channel.evaluate(args.release, suite=suite, backend=backend)
            return dict(evaluation=paired, activation=channel.advance(args.release)), 0
        if name == "advance":
            return channel.advance(args.candidate), 0
        if name == "worker":
            require(1 <= args.poll_seconds <= 60, "worker poll interval must be 1 to 60 seconds")
            while True:
                try:
                    result = channel.advance()
                except ConflictError:
                    result = dict(status="concurrent_controller")
                if args.once:
                    return result, 0
                time.sleep(args.poll_seconds)
        if name == "rollback":
            return channel.rollback(args.release, reason=args.reason), 0
        return channel.record_signal(kind=args.kind, key=args.key, source=args.source), 0
