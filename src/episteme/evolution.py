"""Separate, bounded release channel for advice and isolated Python overlays.

The Store here is independent of scientific history. Candidates never receive
the ledger, the evaluator's expectations, or any authority in the kernel.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from .evolution_docker import DockerBackend, EvaluationError
from .runner_backend import plain_file
from .store import IntegrityError, Store, _canonical_json, canonical, digest


ROLES = frozenset({"analyst", "critic", "developer", "reviewer", "tester", "orchestrator"})
_PATCH_FILES = frozenset({"research.py", "research_loop.py", "research_agents.py", "research_policy.py", "stages.py",
                          "agent_controller.py", "batch_controller.py", "analysis_controller.py"})
_PACK_PATCH = re.compile(r"domains/packs/[a-z][a-z0-9_]+/(analysis|capture|model|programs|compiler)\.py")
_SIGNALS = frozenset({"verified_failure", "measured_resource_loss"})
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value: Any, limit: int) -> bool:
    return type(value) is str and bool(value.strip()) and len(value.encode("utf-8")) <= limit


def validate_profile(profile: Any) -> dict[str, Any]:
    _require(type(profile) is dict and set(profile) == {"role_instructions"},
             "evolution profile must contain only role_instructions")
    instructions = profile["role_instructions"]
    _require(type(instructions) is dict and set(instructions) <= ROLES,
             "unsupported instruction role")
    _require(all(_text(value, 4096) for value in instructions.values()),
             "role instructions must be nonempty strings of at most 4096 bytes")
    return dict(role_instructions=dict(instructions))


def _patch_path(path: Any) -> None:
    _require(type(path) is str and path.startswith("src/episteme/"),
             "patch path must be relative to src/episteme")
    relative = path.removeprefix("src/episteme/")
    _require(relative in _PATCH_FILES or _PACK_PATCH.fullmatch(relative) is not None,
             f"patch path is outside the allowlist: {path}")


def validate_patch(patch: Any) -> dict[str, str]:
    _require(type(patch) is dict and len(patch) <= 12, "patch must contain at most 12 Python overlays")
    for path, source in patch.items():
        _patch_path(path)
        _require(_text(source, 64 * 1024), "replacement Python source is empty or too large")
        try:
            compile(source, path, "exec")  # Syntax only; the host never imports candidate code.
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ValueError(f"patch contains invalid Python at {path}") from exc
    _require(len(canonical(patch)) <= 512 * 1024, "patch exceeds total byte limit")
    return dict(patch)


def validate_suite(suite: Any) -> dict[str, Any]:
    _require(type(suite) is dict and set(suite) == {"suite_id", "cases"}
             and _text(suite["suite_id"], 128), "invalid paired evaluation suite")
    cases = suite["cases"]
    _require(type(cases) is list and 2 <= len(cases) <= 24, "suite needs 2 to 24 paired cases")
    ids = set()
    for case in cases:
        _require(type(case) is dict and set(case) == {"id", "input", "expected", "critical"}
                 and _text(case["id"], 128) and type(case["critical"]) is bool
                 and type(case["input"]) is dict and type(case["expected"]) is dict,
                 "each case needs id, input, expected object, and critical flag")
        _require(case["id"] not in ids and len(canonical(case)) <= 64 * 1024,
                 "duplicate case ID or oversized case")
        ids.add(case["id"])
    _require(any(case["critical"] for case in cases), "suite needs a critical invariant case")
    return _canonical_json(canonical(suite).decode())


def _read(store: Store, key: str) -> Any:
    return _canonical_json(store.read(key).decode())


def _score(store: Store, suite: dict[str, Any], pairs: list[dict[str, Any]]) -> dict[str, Any]:
    _require(type(pairs) is list and len(pairs) == len(suite["cases"]), "paired result is incomplete")
    baseline_correct = 0
    candidate_correct = 0
    errors = []
    critical = []
    regressions = []
    for case, pair in zip(suite["cases"], pairs, strict=True):
        _require(type(pair) is dict and set(pair) == {"id", "baseline", "candidate"}
                 and pair["id"] == case["id"], "paired result does not match its fixed suite")
        correct = []
        for side in ("baseline", "candidate"):
            run = pair[side]
            _require(type(run) is dict and set(run) == {"output", "error"}
                     and ((run["output"] is None) != (run["error"] is None)), "invalid paired observation")
            if run["error"] is not None:
                _require(_text(run["error"], 2048), "invalid worker error")
                errors.append(dict(case=case["id"], side=side))
                correct.append(False)
            else:
                output = _read(store, run["output"])
                _require(type(output) is dict, "worker observation must be a JSON object")
                correct.append(canonical(output) == canonical(case["expected"]))
        baseline, candidate = correct
        baseline_correct += baseline
        candidate_correct += candidate
        if baseline and not candidate:
            regressions.append(case["id"])
        if case["critical"] and not candidate:
            critical.append(case["id"])
    gain = candidate_correct - baseline_correct
    return dict(eligible=not errors and not critical and not regressions and gain > 0,
                baseline_correct=baseline_correct, candidate_correct=candidate_correct,
                correctness_gain=gain, errors=errors, critical_failures=critical,
                correctness_regressions=regressions)


class EvolutionChannel:
    """Append-only candidate, paired evaluation, activation, and rollback history."""

    def __init__(self, root: str | Path, *, read_only: bool = False):
        self.store = Store(root, read_only=read_only)

    def __enter__(self) -> EvolutionChannel:
        return self

    def __exit__(self, *args: Any) -> None:
        self.store.close()

    def _append(self, kind: str, payload: dict[str, Any], revision: int) -> str:
        event = self.store.append(id=f"evolution-{uuid4().hex}", kind=f"evolution_{kind}",
                                  actor="evolution-service", role="release-controller",
                                  payload=payload, expected_revision=revision)
        return event["id"]

    def _state(self) -> dict[str, Any]:
        history = self.store.events()
        active = None
        releases, candidates, evaluations, results = {}, {}, {}, {}
        signals: dict[tuple[str, str, str], set[str]] = {}
        acted = set()
        quarantined = set()
        configurations, campaigns, campaign_results = {}, {}, {}
        for event in history:
            key, payload = event["id"], event["payload"]
            kind = event["kind"]
            if (event["actor"], event["role"], event["schema_version"]) != (
                    "evolution-service", "release-controller", 1):
                raise IntegrityError("root is not a dedicated evolution channel")
            if kind == "evolution_initialized":
                _require(event["seq"] == 1, "channel was initialized twice")
                validate_profile(_read(self.store, payload["profile"]))
                validate_patch(_read(self.store, payload["patch"]))
                releases[key] = dict(**payload, release_hash=event["hash"])
                active = key
            elif kind == "evolution_signal":
                _require(active is not None and payload["release"] == active, "stale signal release")
                record = (active, payload["kind"], payload["key"])
                values = signals.setdefault(record, set())
                _require(payload["source"] not in values, "duplicate signal source")
                values.add(payload["source"])
            elif kind == "evolution_configured":
                from .evolution_gepa import case_inputs, freeze_search

                _require(active is not None and payload["release"] == active and active not in configurations
                         and not any(candidate["parent"] == active for candidate in candidates.values()),
                         "campaign must be frozen before candidate proposals")
                configuration = _read(self.store, payload["config"])
                search = freeze_search(backend=DockerBackend(**configuration["backend"]),
                                       **configuration["search"])
                suite = validate_suite(configuration["suite"])
                _require(not case_inputs(suite) & (case_inputs(search["training"]) | case_inputs(search["validation"])),
                         "closed campaign suite overlaps optimization cases")
                configurations[active] = dict(**payload, event=key)
            elif kind == "evolution_campaign_started":
                trigger = (active, payload["kind"], payload["key"])
                sources = payload["sources"]
                _require(active is not None and active == payload["release"] and active in configurations
                         and payload["config"] == configurations[active]["event"] and active not in campaigns
                         and len(set(sources)) == 2 and set(sources) <= signals.get(trigger, set()),
                         "campaign launch lacks a configuration or repeated evidence")
                campaigns[active] = dict(**payload, event=key)
            elif kind == "evolution_campaign_finished":
                release = payload["release"]
                _require(release in campaigns and release not in campaign_results
                         and payload["campaign"] == campaigns[release]["event"], "invalid campaign completion")
                campaign_results[release] = dict(**payload, event=key)
            elif kind == "evolution_candidate":
                _require(active is not None and payload["parent"] == active, "stale candidate parent")
                validate_profile(_read(self.store, payload["profile"]))
                validate_patch(_read(self.store, payload["patch"]))
                if payload["optimization"] is not None:
                    _read(self.store, payload["optimization"])
                candidates[key] = dict(**payload, proposal_hash=event["hash"])
            elif kind == "evolution_evaluation_started":
                target = payload["target"]
                health = payload["health"]
                if health:
                    _require(target == active and releases[target]["parent"] == payload["baseline"]
                             and payload["baseline"] is not None and releases[target]["backend"] == payload["backend"],
                             "invalid active release health check")
                else:
                    _require(target in candidates and candidates[target]["parent"] == active == payload["baseline"],
                             "evaluation has no current candidate parent")
                suite = validate_suite(_read(self.store, payload["suite"]))
                if not health and active in configurations:
                    config = _read(self.store, configurations[active]["config"])
                    _require(suite == config["suite"] and payload["backend"] == config["backend"],
                             "evaluation differs from the frozen campaign suite or backend")
                DockerBackend(**payload["backend"])
                evaluations[key] = payload
            elif kind == "evolution_evaluation_finished":
                attempt = payload["evaluation"]
                _require(attempt in evaluations and attempt not in results, "invalid evaluation completion")
                frozen = evaluations[attempt]
                suite = _read(self.store, frozen["suite"])
                pairs = _read(self.store, payload["pairs"])
                _require(payload["score"] == _score(self.store, suite, pairs), "paired score differs from evidence")
                results[attempt] = dict(**payload, event=key)
            elif kind == "evolution_activated":
                attempt = payload["evaluation"]
                source = evaluations[attempt]
                candidate = candidates[source["target"]]
                latest = next(id for id, evaluation in reversed(list(evaluations.items()))
                              if evaluation["target"] == source["target"] and not evaluation["health"])
                _require(not source["health"] and results[attempt]["score"]["eligible"]
                         and active == source["baseline"] == candidate["parent"] and attempt not in acted
                         and latest == attempt and self._version(candidate, source["backend"]) not in quarantined,
                         "release activation lacks an eligible current evaluation")
                releases[key] = dict(**candidate, backend=source["backend"], evaluation=attempt,
                                     release_hash=event["hash"])
                acted.add(attempt)
                active = key
            elif kind == "evolution_rolled_back":
                _require(active == payload["release"] and releases[active]["parent"] == payload["restored"]
                         and payload["restored"] is not None, "rollback does not restore the prior release")
                if payload["evaluation"] is not None:
                    attempt = payload["evaluation"]
                    _require(evaluations[attempt]["health"] and evaluations[attempt]["target"] == active
                             and self._critical_regression(results[attempt]["score"]), "rollback lacks a critical regression")
                    acted.add(attempt)
                    release = releases[active]
                    quarantined.add(self._version(release, release["backend"]))
                active = payload["restored"]
            else:
                raise IntegrityError(f"unexpected release channel event {kind}")
        return dict(revision=len(history), active=active, releases=releases, candidates=candidates,
                    evaluations=evaluations, results=results, acted=acted, signals=signals,
                    quarantined=quarantined, configurations=configurations, campaigns=campaigns,
                    campaign_results=campaign_results)

    def initialize(self, profile: dict[str, Any], base_commit: str | None = None) -> str:
        state = self._state()
        _require(state["active"] is None, "evolution channel is already initialized")
        _require(base_commit is None or (type(base_commit) is str and _GIT_COMMIT.fullmatch(base_commit)),
                 "base_commit must be a full Git commit ID")
        payload = dict(profile=self.store.put_json(validate_profile(profile)), patch=self.store.put_json({}),
                       base_commit=base_commit, parent=None, backend=None, evaluation=None)
        return self._append("initialized", payload, state["revision"])

    def record_signal(self, *, kind: str, key: str, source: str,
                      expected_release: str | None = None) -> dict[str, Any]:
        _require(kind in _SIGNALS and _text(key, 128)
                 and type(source) is str and re.fullmatch(r"[0-9a-f]{64}", source) is not None,
                 "signal needs a known kind, a grouping key and an evidence SHA-256")
        state = self._state()
        active = state["active"]
        _require(active is not None, "evolution channel is not initialized")
        if expected_release is not None and expected_release != active:
            return dict(recorded=False, observations=0, triggered=False, status="inactive_release")
        previous = state["signals"].get((active, kind, key), set())
        if source in previous:
            return dict(recorded=False, observations=len(previous), triggered=len(previous) >= 2)
        self._append("signal", dict(release=active, kind=kind, key=key, source=source), state["revision"])
        return dict(recorded=True, observations=len(previous) + 1, triggered=len(previous) + 1 >= 2)

    def configure(self, *, options: dict[str, list[str]], training: dict[str, Any], validation: dict[str, Any],
                  suite: dict[str, Any], backend: DockerBackend, max_evaluations: int = 24, seed: int = 0) -> str:
        """Freeze one bounded automatic profile campaign for the current release."""
        from .evolution_gepa import case_inputs, freeze_search

        state = self._state()
        active = state["active"]
        _require(active is not None and active not in state["configurations"]
                 and not any(row["parent"] == active for row in state["candidates"].values()),
                 "campaign must be frozen before candidate proposals")
        search = freeze_search(options=options, training=training, validation=validation,
                               backend=backend, max_evaluations=max_evaluations, seed=seed)
        frozen = validate_suite(suite)
        _require(not case_inputs(frozen) & (case_inputs(search["training"]) | case_inputs(search["validation"])),
                 "closed campaign suite overlaps optimization cases")
        pin = self._pin(active, state)
        _require(pin["backend"] in (None, backend.descriptor), "campaign must use the pinned backend")
        instructions = pin["profile"]["role_instructions"]
        _require(any(advice != instructions.get(role) for role, entries in options.items() for advice in entries),
                 "campaign has no alternative instruction")
        config = self.store.put_json(dict(search=search, suite=frozen, backend=backend.descriptor))
        return self._append("configured", dict(release=active, config=config), state["revision"])

    def _pin(self, key: str, state: dict[str, Any]) -> dict[str, Any]:
        entry = state["releases"].get(key) or state["candidates"].get(key)
        _require(entry is not None, "unknown evolution release or candidate")
        return dict(release=key, profile=_read(self.store, entry["profile"]),
                    patch=_read(self.store, entry["patch"]), base_commit=entry["base_commit"],
                    backend=entry.get("backend"), release_hash=entry.get("release_hash"),
                    profile_digest=entry["profile"], patch_digest=entry["patch"])

    def pin(self, release: str | None = None) -> dict[str, Any]:
        state = self._state()
        key = state["active"] if release is None else release
        _require(key in state["releases"], "unknown evolution release")
        return self._pin(key, state)

    def is_quarantined(self, release: str) -> bool:
        state = self._state()
        entry = state["releases"].get(release)
        _require(entry is not None, "unknown evolution release")
        backend = entry["backend"]
        return backend is not None and self._version(entry, backend) in state["quarantined"]

    def propose(self, *, profile: dict[str, Any] | None = None, patch: dict[str, str] | None = None,
                provenance: str, expected_parent: str | None = None) -> str:
        return self._propose(profile=profile, patch=patch, provenance=provenance,
                             expected_parent=expected_parent, optimization=None)

    def propose_worktree(self, worktree: str | Path, *, provenance: str) -> str:
        """Read allowlisted replacements from a separate linked Git worktree."""
        base = self.pin()
        commit = base["base_commit"]
        _require(commit is not None, "Python overlays need a pinned Git baseline")
        root = Path(worktree).resolve(strict=True)

        def git(*args: str) -> str:
            result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                    check=False, timeout=15)
            _require(result.returncode == 0, "Git could not read the candidate worktree")
            return result.stdout.decode("utf-8")

        git_dir = Path(git("rev-parse", "--absolute-git-dir").strip()).resolve()
        common_dir = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
        _require(Path(git("rev-parse", "--show-toplevel").strip()).resolve() == root
                 and git_dir.parent == common_dir / "worktrees", "a separate linked Git worktree is required")
        _require(not git("ls-files", "--others", "--exclude-standard", "-z"),
                 "candidate worktree contains untracked files")
        changes = git("diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", commit, "--")
        patch = {}
        for name in changes.split("\0")[:-1]:
            _patch_path(name)
            path = root / name
            _require(path.resolve() == path and path.is_file() and plain_file(path)
                     and path.stat().st_size <= 64 * 1024, "patch target is not a bounded regular file")
            with path.open("rb") as source:
                data = source.read(64 * 1024 + 1)
            _require(len(data) <= 64 * 1024, "patch grew while being read")
            patch[name] = data.decode("utf-8")
        return self.propose(patch=patch, provenance=provenance, expected_parent=base["release"])

    def _propose(self, *, profile: dict[str, Any] | None, patch: dict[str, str] | None,
                 provenance: str, expected_parent: str | None, optimization: str | None) -> str:
        _require(_text(provenance, 2048), "candidate needs bounded provenance")
        state = self._state()
        parent = state["active"]
        _require(parent is not None and (expected_parent is None or parent == expected_parent),
                 "candidate must be based on the current active release")
        base = self._pin(parent, state)
        desired = base["profile"] if profile is None else validate_profile(profile)
        overlays = base["patch"] if patch is None else validate_patch({**base["patch"], **validate_patch(patch)})
        _require(desired != base["profile"] or overlays != base["patch"], "candidate has no changes")
        _require(not overlays or base["base_commit"] is not None, "a Python overlay needs a pinned Git baseline")
        payload = dict(parent=parent, profile=self.store.put_json(desired), patch=self.store.put_json(overlays),
                       base_commit=base["base_commit"], provenance=provenance, optimization=optimization)
        return self._append("candidate", payload, state["revision"])

    def _compare(self, key: str, *, suite: dict[str, Any], backend: DockerBackend, health: bool) -> dict[str, Any]:
        _require(type(backend) is DockerBackend, "release evaluations require the Docker backend")
        frozen_suite = validate_suite(suite)
        state = self._state()
        if health:
            _require(key == state["active"] and key in state["releases"], "health check needs the active release")
            release = state["releases"][key]
            baseline = release["parent"]
            _require(baseline is not None and release["backend"] == backend.descriptor,
                     "health check needs the release's pinned backend and parent")
        else:
            _require(key in state["candidates"], "unknown candidate")
            target = state["candidates"][key]
            baseline = target["parent"]
            _require(baseline == state["active"], "candidate parent is stale")
            if baseline in state["configurations"]:
                config = _read(self.store, state["configurations"][baseline]["config"])
                _require(frozen_suite == config["suite"] and backend.descriptor == config["backend"],
                         "evaluation differs from the frozen campaign suite or backend")
            if target["optimization"] is not None:
                used = set(_read(self.store, target["optimization"])["case_inputs"])
                _require(not used & {digest(canonical(case["input"])) for case in frozen_suite["cases"]},
                         "closed paired suite contains an optimization case")
        _require(not any(item["target"] == key and attempt not in state["results"]
                         for attempt, item in state["evaluations"].items()),
                 "an evaluation has unknown completion and cannot be relaunched")
        pins = {"baseline": self._pin(baseline, state), "candidate": self._pin(key, state)}
        for pin in pins.values():
            _require(pin["backend"] in (None, backend.descriptor), "paired evaluation must use the pinned backend")
        payload = dict(target=key, baseline=baseline, backend=backend.descriptor, health=health,
                       suite=self.store.put_json(frozen_suite))
        evaluation = self._append("evaluation_started", payload, state["revision"])
        pairs = []
        for index, case in enumerate(frozen_suite["cases"]):
            pair = dict(id=case["id"])
            order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
            for side in order:
                try:
                    output = backend.invoke("evaluate", case["input"], pins[side])
                    pair[side] = dict(output=self.store.put_json(output), error=None)
                except EvaluationError as exc:
                    pair[side] = dict(output=None, error=str(exc))
            pairs.append(pair)
        score = _score(self.store, frozen_suite, pairs)
        completion = dict(evaluation=evaluation, pairs=self.store.put_json(pairs), score=score)
        state = self._state()
        event = self._append("evaluation_finished", completion, state["revision"])
        return dict(**completion, event=event)

    def evaluate(self, candidate: str, *, suite: dict[str, Any], backend: DockerBackend) -> dict[str, Any]:
        return self._compare(candidate, suite=suite, backend=backend, health=False)

    def health_check(self, release: str, *, suite: dict[str, Any], backend: DockerBackend) -> dict[str, Any]:
        result = self._compare(release, suite=suite, backend=backend, health=True)
        if self._critical_regression(result["score"]):
            state = self._state()
            if state["active"] == release:
                result["transition"] = self._rollback(
                    state, release, "paired health check found a critical regression", result["evaluation"])
        return result

    @staticmethod
    def _critical_regression(score: dict[str, Any]) -> bool:
        return bool(set(score["critical_failures"]) & set(score["correctness_regressions"]))

    @staticmethod
    def _version(entry: dict[str, Any], backend: dict[str, Any]) -> tuple[str, str, str | None, str]:
        return entry["profile"], entry["patch"], entry["base_commit"], digest(canonical(backend))

    def _rollback(self, state: dict[str, Any], release: str, reason: str,
                  evaluation: str | None) -> dict[str, Any]:
        _require(_text(reason, 2048) and state["active"] == release and release in state["releases"],
                 "rollback needs a reason and the current active release")
        restored = state["releases"][release]["parent"]
        _require(restored is not None, "the initial release has no parent to restore")
        event = self._append("rolled_back", dict(release=release, restored=restored,
                                                reason=reason, evaluation=evaluation), state["revision"])
        return dict(status="rolled_back", release=restored, event=event)

    def rollback(self, release: str, *, reason: str) -> dict[str, Any]:
        return self._rollback(self._state(), release, reason, None)

    def advance(self, candidate: str | None = None) -> dict[str, Any]:
        state = self._state()
        active = state["active"]
        for attempt, result in reversed(list(state["results"].items())):
            source = state["evaluations"][attempt]
            if source["health"] and source["target"] == active and self._critical_regression(result["score"]):
                return self._rollback(state, active, "paired health check found a critical regression", attempt)
        # Consider only the latest attempt for each candidate, even if pending or failed.
        seen = set()
        for attempt, source in reversed(list(state["evaluations"].items())):
            target = source["target"]
            if (source["health"] or source["baseline"] != active or target in seen
                    or (candidate is not None and target != candidate)):
                continue
            seen.add(target)
            result = state["results"].get(attempt)
            candidate_version = self._version(state["candidates"][target], source["backend"])
            if (attempt not in state["acted"] and result is not None and result["score"]["eligible"]
                    and candidate_version not in state["quarantined"]):
                event = self._append("activated", dict(evaluation=attempt), state["revision"])
                return dict(status="activated", release=event, candidate=target)
        if candidate is None and active in state["configurations"] and active not in state["campaigns"]:
            for (release, kind, key), sources in state["signals"].items():
                if release == active and len(sources) >= 2:
                    return self._run_campaign(state, kind, key, sorted(sources)[:2])
        return dict(status="no_eligible_candidate", release=active)

    def _run_campaign(self, state: dict[str, Any], kind: str, key: str, sources: list[str]) -> dict[str, Any]:
        from .evolution_gepa import optimize_instructions, require_gepa

        active = state["active"]
        try:
            require_gepa()
        except ValueError as exc:
            return dict(status="gepa_unavailable", release=active, reason=str(exc))
        frozen = state["configurations"][active]
        config = _read(self.store, frozen["config"])
        backend = DockerBackend(**config["backend"])
        campaign = self._append("campaign_started", dict(release=active, config=frozen["event"],
                                                        kind=kind, key=key, sources=sources), state["revision"])
        result: dict[str, Any] = {"status": "blocked", "release": active}
        try:
            selected = optimize_instructions(self, backend=backend, **config["search"], expected_parent=active)
            result["optimization"] = selected
            candidate = selected["candidate"]
            if candidate is None:
                result["status"] = "no_improvement"
            else:
                result["evaluation"] = self.evaluate(candidate, suite=config["suite"], backend=backend)
                result.update(self.advance(candidate))
        except ValueError as exc:
            result["reason"] = str(exc)[:2048]
        current = self._state()
        self._append("campaign_finished", dict(campaign=campaign, release=active, result=result), current["revision"])
        return dict(**result, campaign=campaign)

    def status(self) -> dict[str, Any]:
        state = self._state()
        active = state["active"]
        return dict(active=active, revision=state["revision"],
                    configuration=state["configurations"].get(active, {}).get("event"),
                    campaigns=[dict(**entry, completion=state["campaign_results"].get(release))
                               for release, entry in state["campaigns"].items()],
                    candidates=[dict(id=key, parent=entry["parent"], provenance=entry["provenance"])
                                for key, entry in state["candidates"].items()],
                    evaluations=[dict(id=key, target=entry["target"], health=entry["health"],
                                      result=state["results"].get(key))
                                 for key, entry in state["evaluations"].items()],
                    triggers=[dict(kind=kind, key=key, observations=len(sources))
                              for (release, kind, key), sources in state["signals"].items()
                              if release == active and len(sources) >= 2])
