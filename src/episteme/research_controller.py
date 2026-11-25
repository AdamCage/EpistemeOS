"""Run a bounded research session through the existing planning and evidence APIs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import os
import stat
from typing import Any, Iterator
from uuid import uuid4

from .codex_provider import freeze_provider
from .domain_packs import execution_fields, store_capture
from .domains import registry
from .execution import freeze_environment, reconcile_job
from .kernel import Actor, Kernel, require
from .research import deadline, remaining_seconds, research_index, review_key, session_view, _read, identity
from .research_agents import INSTRUCTIONS, capture_worker, launch_worker, prepare_worker
from .research_literature import project_literature_result, retrieve_literature, validate_literature_action
from .search import COMPONENTS
from .stages import _roster_size, stage_run, stage_status
from .store import ConflictError, Store, canonical, digest


PLANNER = "research-orchestrator"
ANALYST = "research-analyst"
EXECUTOR = "research-executor"
REANALYST = "research-reanalyst"


def _execute(store: Store, *, study: str, correlation: str, action: str, payload: dict[str, Any],
             actor: str, role: str, cause: str | None, command_id: str, revision: int) -> Any:
    from .commands import CommandService

    return CommandService(store).execute(dict(
        context=dict(command_id=command_id, expected_revision=revision, actor=actor, role=role,
                     study_id=study, correlation_id=correlation, causation_id=cause),
        request=dict(version=1, action=action, payload=payload)))


def start_research(store: Store, hypothesis: str, config: dict[str, Any], *, author: str = "author",
                   release_root: Path | None = None, base_dir: Path | None = None) -> dict[str, Any]:
    required = {"budget", "scope", "candidates", "literature"}
    require(type(config) is dict and required <= set(config) <= required | {"model", "reasoning_effort"},
            "invalid research configuration fields")
    require(type(config["candidates"]) is list and 1 <= len(config["candidates"]) <= 12,
            "1 to 12 research candidates required")
    candidates = []
    recipe = {"label", "pack_id", "parameters", "host_inputs", "phase", "purpose"}
    for item in config["candidates"]:
        require(type(item) is dict and recipe <= set(item) <= recipe | {"capture_source"},
                "invalid research candidate fields")
        require(type(item["pack_id"]) is str and type(item.get("capture_source", "")) in {str, type(None)},
                "invalid research candidate pack or capture path")
        pack = registry.load_pack(item["pack_id"])
        capture = None
        if item.get("capture_source") is not None:
            source = (base_dir or Path.cwd()) / Path(item["capture_source"])
            capture = store_capture(store, pack.pack_id, source)["capture"]
        candidates.append(dict({key: item[key] for key in recipe}, pack_version=pack.pack_version,
                               pack_code_digest=pack.pack_code_digest, capture=capture))
    pin = None
    instructions = dict(INSTRUCTIONS)
    if release_root is not None:
        from .evolution import EvolutionChannel

        release_root = release_root.resolve()
        require(not release_root.is_relative_to(store.root) and not store.root.is_relative_to(release_root),
                "release channel and research state must be separate directories")
        with EvolutionChannel(release_root, read_only=True) as channel:
            pin = channel.pin()
        for role, advice in pin["profile"]["role_instructions"].items():
            instructions[role] += "\nRelease guidance: " + advice
    provider = None
    if config.get("model") is not None:
        provider = freeze_provider(store, model=config["model"], reasoning_effort=config.get("reasoning_effort", "low"))
    require(type(config["literature"]) is list, "research literature actions must be a list")
    contract = store.put_json(dict(budget=config["budget"], scope=config["scope"], candidates=candidates,
                                  literature=[validate_literature_action(row) for row in config["literature"]],
                                  provider=provider, environment=freeze_environment(store),
                                  instructions=store.put_json(instructions), release=store.put_json(pin) if pin else None,
                                  release_root=str(release_root) if pin else None))
    study = f"research-{uuid4().hex}"
    session = _execute(store, study=study, correlation=study, action="research.begin",
                       payload=dict(hypothesis=hypothesis, contract=contract), actor=author,
                       role="author", cause=None, command_id=study, revision=len(store.events()))
    return research_status(store, session)


def discuss_research(store: Store, session: str, reply: str, *, author: str = "author") -> dict[str, Any]:
    _record(store, session, "reply", "reply", dict(text=reply), author=author)
    return research_status(store, session)


def _record(store: Store, session: str, key: str, step: str, data: dict[str, Any], *,
            author: str | None = None) -> str:
    history = store.events()
    opened = Kernel._get(history, session, "research_session")
    return _execute(store, study=opened["payload"]["study_id"], correlation=session,
                    action="research.record", payload=dict(session=session, key=key, step=step, data=data),
                    actor=author or PLANNER, role="author" if author is not None else "planner",
                    cause=session, command_id=f"research-note-{uuid4().hex}", revision=len(history))


def _command_id(session: str, label: str) -> str:
    return "research-work-" + digest(canonical([session, label]))


def _result(receipts: list[dict[str, Any]], session: str, label: str) -> Any:
    row = next((row for row in receipts if row["command_id"] == _command_id(session, label)), None)
    return row["result"] if row is not None else None


def _once(store: Store, session: str, label: str, action: str, payload: dict[str, Any], *,
          cause: str | None = None, actor: str = PLANNER, role: str = "planner") -> Any:
    history, receipts = store.snapshot()
    command_id = _command_id(session, label)
    existing = next((item for item in receipts if item["command_id"] == command_id), None)
    if existing is not None:
        require(existing["request"]["action"] == action and existing["context"]["correlation_id"] == session,
                "wrong research command receipt")
        return existing["result"]
    opened = Kernel._get(history, session, "research_session")
    return _execute(store, study=opened["payload"]["study_id"], correlation=session, action=action,
                    payload=payload, actor=actor, role=role, cause=cause or session,
                    command_id=command_id, revision=len(history))


@contextmanager
def _exclusive_run(root: Path) -> Iterator[None]:
    """OS-released lock serializes local controllers, including stage dispatches."""
    path = root / "research-worker.lock"
    if path.exists() or path.is_symlink():
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400),
                "research lock must be a plain file")
    with path.open("a+b") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ConflictError("a local research worker already owns this store") from exc
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _related(history: list[dict[str, Any]], receipts: list[dict[str, Any]], session: str,
             kind: str) -> list[dict[str, Any]]:
    ids = {event for row in receipts if row["context"]["correlation_id"] == session for event in row["event_ids"]}
    return [row for row in history if row["kind"] == kind and row["id"] in ids]


def _stage_closes(history: list[dict[str, Any]], receipts: list[dict[str, Any]],
                  session: str) -> list[dict[str, Any]]:
    return _related(history, receipts, session, "stage_close")


def _literature_projection(store: Store, view: dict[str, Any], *, include_text: bool = False) -> list[dict[str, Any]]:
    rows = []
    for row in view["rows"]:
        payload = row["payload"]
        if payload["step"] == "literature":
            data = payload["data"]
            entry = data["result"] or dict(failure=data["failure"])
            if include_text and data["result"]:
                entry = project_literature_result(store, data["result"])
            rows.append(dict(entry, id=row["id"]))
    return rows


def _verification(history: list[dict[str, Any]], closed: dict[str, Any]) -> dict[str, Any]:
    batch = closed["payload"]["batch"]
    slot_runs = {row["payload"]["run"]: row["payload"]["slot"] for row in history[:closed["seq"] - 1]
                 if row["kind"] == "batch_slot" and row["payload"]["batch"] == batch}
    results = [row for row in history[:closed["seq"] - 1] if row["kind"] == "result"
               and row["payload"]["run"] in slot_runs and row["payload"]["status"] == "completed"]
    seeds = sorted(int(slot_runs[row["payload"]["run"]].removeprefix("primary:")) for row in results
                   if slot_runs[row["payload"]["run"]].startswith("primary:"))
    return dict(completed_primary_seeds=seeds, fresh_seed_repetition=len(seeds) > 1,
                same_data_reanalysis=[row["id"] for row in results
                                      if slot_runs[row["payload"]["run"]].startswith("reanalysis:")],
                same_code_replays=[row["id"] for row in history[:closed["seq"] - 1]
                                   if row["kind"] == "execution_reproduction" and row["payload"]["run"] in slot_runs],
                independent_reanalysis="not_established", new_data_replication="not_established")


def research_status(store: Store, session: str) -> dict[str, Any]:
    history, receipts = store.snapshot()
    research_index(store, history, receipts)
    view = session_view(history, session)
    opened, steps = view["opened"], view["steps"]
    contract = _read(store, opened["payload"]["contract"])
    budget = contract["budget"]
    closed = _stage_closes(history, receipts, session)
    stages = _related(history, receipts, session, "stage_open")
    batches = _related(history, receipts, session, "batch_plan")
    selections = _related(history, receipts, session, "search_selection")
    attempt_reserves = sum(len(row["payload"]["slots"]) for row in batches)
    model_calls = sum(row["payload"]["step"] == "task-intent" for row in view["rows"]) if contract["provider"] else 0
    accepted = steps.get("accept")
    finish = steps.get("finish")
    selected = selections[-1]["payload"] if selections else None
    status = "closed" if finish else "running" if accepted else "draft"
    wall_remaining = budget["wall_seconds"]
    if finish:
        wall_remaining = max(0, int(deadline(accepted, budget) - datetime.fromisoformat(finish["created_at"]).timestamp()))
    elif accepted:
        wall_remaining = remaining_seconds(accepted, budget)
    result = dict(session=session, study=opened["payload"]["study_id"], status=status,
                  draft_revision=(view["rows"][-1] if view["rows"] else opened)["hash"] if not accepted else None,
                  hypothesis=opened["payload"]["hypothesis"], questions=opened["payload"]["questions"],
                  replies=[row["payload"]["data"]["text"] for row in view["rows"] if row["payload"]["step"] == "reply"],
                  contract=opened["payload"]["contract"], candidates=[{key: row[key] for key in ("label", "phase", "purpose")}
                                                                   for row in contract["candidates"]],
                  approved_scope=contract["scope"], budget=budget, literature_plan=contract["literature"],
                  model=_read(store, contract["provider"])["model"] if contract["provider"] else None,
                  literature=_literature_projection(store, view),
                  stage_results=[dict(row["payload"], close=row["id"], verification=_verification(history, row))
                                 for row in closed],
                  decision=dict(protocol=selected["protocol"], reason=selected["reason"]) if selected else None,
                  remaining=dict(stages=max(0, budget["stages"] - len(stages)),
                                 attempts=max(0, budget["attempts"] - attempt_reserves),
                                 model_calls=max(0, budget["model_calls"] - model_calls),
                                 literature_calls=max(0, budget["literature_calls"] - sum(
                                     row["payload"]["step"] == "literature-intent" for row in view["rows"])),
                                 wall_seconds=wall_remaining),
                  scientific_validity="not_assessed", fixture_provider=contract["provider"] is None,
                  release=_read(store, contract["release"])["release"] if contract["release"] else None)
    if finish:
        result["disposition"] = finish["payload"]["data"]
    for name in ("analyst", "critic"):
        if f"task:{name}" in steps:
            result[name] = steps[f"task:{name}"]["payload"]["data"]["result"]
    return result


class _Stop(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


class _AwaitCompletion(Exception):
    pass


class _Run:
    def __init__(self, store: Store, session: str, *, fetch: Any = None):
        self.store, self.session, self.fetch = store, session, fetch
        self.snapshot()
        self.opened = self.view["opened"]
        self.contract = _read(store, self.opened["payload"]["contract"])
        self.instructions = _read(store, self.contract["instructions"])
        self.budget = self.contract["budget"]

    def snapshot(self) -> None:
        self.history, self.receipts = self.store.snapshot()
        self.view = session_view(self.history, self.session)

    def step(self, key: str) -> dict[str, Any] | None:
        return self.view["steps"].get(key)

    def record(self, key: str, step: str, data: dict[str, Any]) -> str:
        result = _record(self.store, self.session, key, step, data)
        self.snapshot()
        return result

    def once(self, label: str, command_action: str, **payload: Any) -> Any:
        result = _once(self.store, self.session, label, command_action, payload)
        self.snapshot()
        return result

    def seconds(self) -> int:
        available = remaining_seconds(self.view["steps"]["accept"], self.budget)
        if available < 1:
            raise _Stop("budget_exhausted", "Study wall-time budget exhausted.")
        return available

    def release_gate(self) -> None:
        if self.contract["release"] is None:
            return
        from .evolution import EvolutionChannel

        pin = _read(self.store, self.contract["release"])
        root = Path(self.contract["release_root"])
        if not (root / "state.sqlite3").is_file():
            raise _Stop("blocked", "Pinned local release channel is unavailable.")
        with EvolutionChannel(root, read_only=True) as channel:
            require(channel.pin(pin["release"])["release_hash"] == pin["release_hash"],
                    "local release channel does not match the pinned release")
            if channel.is_quarantined(pin["release"]):
                raise _Stop("blocked", "The pinned release failed a critical health check and is quarantined.")

    def literature(self) -> None:
        for index, action in enumerate(self.contract["literature"]):
            intent, key = f"literature-intent:{index}", f"literature:{index}"
            if self.step(key):
                continue
            if self.step(intent):
                # Never guess whether a dropped request was sent.
                self.record(key, "literature", dict(index=index, result=None, failure="Unknown after interruption"))
                continue
            if action["kind"] != "fixture_source" and self.seconds() < 10:
                raise _Stop("budget_exhausted", "Not enough wall time to start another literature request.")
            self.record(intent, "literature-intent", dict(index=index))
            try:
                result = retrieve_literature(self.store, action, fetch=self.fetch)
                failure = None
            except (OSError, ValueError) as exc:
                result, failure = None, (str(exc) or type(exc).__name__)[:2048]
            self.record(key, "literature", dict(index=index, result=result, failure=failure))

    def role(self, role: str, *, suffix: str = "", references: list[str] | None = None,
             candidates: list[dict[str, Any]] | None = None, **details: Any) -> dict[str, Any]:
        key = role + suffix
        result = self.step(f"task:{key}")
        if result is None:
            intent = self.step(f"task-intent:{key}")
            fresh = intent is None
            if fresh:
                self.release_gate()
                provider = self.contract["provider"]
                if provider is not None and sum(row["payload"]["step"] == "task-intent" for row in self.view["rows"]) >= self.budget["model_calls"]:
                    raise _Stop("budget_exhausted", f"Model-call budget exhausted before {role}.")
                ids = list(dict.fromkeys(references or []))
                events = {row["id"]: row for row in self.history}
                context = dict(session=self.session, role=role, instructions=self.contract["instructions"],
                               hypothesis=self.opened["payload"]["hypothesis"], scope=self.contract["scope"],
                               author_replies=[row["payload"]["data"]["text"] for row in self.view["rows"]
                                               if row["payload"]["step"] == "reply"],
                               references=ids, references_with_hashes=[dict(id=id, hash=events[id]["hash"]) for id in ids],
                               candidates=[{field: item[field] for field in ("label", "pack_id", "phase", "purpose", "parameters")}
                                           for item in (candidates or [])], **details)
                spec = prepare_worker(self.store, provider=provider, role=role, context=context,
                                      instructions=self.instructions[role],
                                      wall_seconds=min(self.seconds(), self.budget["operation_seconds"])) if provider else None
                self.record(f"task-intent:{key}", "task-intent", dict(role=role, context=self.store.put_json(context),
                            spec=self.store.put_json(spec) if spec else None, workspace=uuid4().hex if spec else None))
                intent = self.step(f"task-intent:{key}")
            data = intent["payload"]["data"]
            completion = None
            if data["spec"] is not None:
                workspace = self.store.root / "research-workers" / data["workspace"]
                for path in (workspace.parent, workspace):
                    if path.exists() or path.is_symlink():
                        info = path.lstat()
                        require(stat.S_ISDIR(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400),
                                "research workspace must be a plain directory")
                spec = _read(self.store, data["spec"])
                if fresh:
                    try:
                        launch_worker(self.store, workspace, spec, identity(intent))
                    except OSError as exc:
                        raise _AwaitCompletion(f"{role} dispatch outcome unknown: {exc}") from exc
                completion = capture_worker(self.store, workspace, spec)
                if completion is None:
                    raise _AwaitCompletion(f"{role} dispatch has no confirmed saved completion.")
            self.record(f"task:{key}", "task", dict(intent=intent["id"], completion=completion))
            result = self.step(f"task:{key}")
        report = result["payload"]["data"]["result"]
        if report["status"] != "answered":
            raise _Stop("blocked", f"{role}: {report['reason']}"[:2048])
        return report["answer"]

    def planning(self) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        literature = _literature_projection(self.store, self.view, include_text=True)[:8]
        refs = [row["id"] for row in literature]
        candidates = self.contract["candidates"]
        analyst = self.role("analyst", references=refs, literature=literature)
        refs.append(self.step("task:analyst")["id"])
        first_phase = "exploratory" if any(row["phase"] == "exploratory" for row in candidates) else "confirmatory"
        first = [row for row in candidates if row["phase"] == first_phase]
        critic = self.role("critic", references=refs, candidates=first, explanation=analyst, literature=literature)
        replies = [row["payload"]["data"]["text"] for row in self.view["rows"] if row["payload"]["step"] == "reply"]
        study = self.opened["payload"]["study_id"]
        question = self.once("question", "planning.question", study_id=study,
                             statement=self.opened["payload"]["hypothesis"], scope=self.contract["scope"],
                             objective=replies[0] if replies else "Test whether the specified hypothesis survives its controls.",
                             constraints=replies[1:] or ["Only the frozen research recipes are authorized."],
                             stopping_criteria=["Stop at the approved budget or when the current evidence justifies stopping."])
        hypotheses = [self.once(f"hypothesis-{name}", "kernel.hypothesis", scope=self.contract["scope"], **analyst[name])
                      for name in ("null", "alternative")]
        explanation = self.once("explanation", "planning.explanation_set", question=question, hypotheses=hypotheses,
                                comparison_plan=analyst["comparison"])
        return question, explanation, analyst, critic

    def protocol(self, candidate: dict[str, Any], explanation: str) -> dict[str, Any]:
        label = candidate["label"]
        result = self.once(f"binding-{label}", "pack.preregister", explanation_set=explanation,
                           environment=self.contract["environment"], **{key: candidate[key] for key in (
                               "pack_id", "pack_version", "pack_code_digest", "parameters", "host_inputs", "capture")})
        event = Kernel._get(self.history, result["protocol"], "protocol")
        require(event["payload"]["protocol_mode"] == candidate["phase"],
                f"pinned recipe {label} does not compile to the approved phase")
        if candidate["phase"] == "confirmatory":
            splits = event["payload"]["statistical_design"]["data_splits"]
            holdouts = {row["digest"] for row in splits if row["role"] == "confirmatory"}
            require(bool(holdouts), "confirmatory recipe has no held-out data split")
        return dict(result, roster=_roster_size(event))

    def stage(self, index: int, phase: str, candidates: list[dict[str, Any]], question: str, explanation: str,
              preferred: str, predecessor: str | None, previous_review: dict[str, Any] | None) -> dict[str, Any]:
        label = f"round-{index}"
        use = [row for row in candidates if row["phase"] == phase]
        batch_id = _result(self.receipts, self.session, f"{label}-batch")
        reserved = sum(len(row["payload"]["slots"]) for row in _related(self.history, self.receipts, self.session, "batch_plan"))
        budget = self.budget["attempts"] - reserved
        scores = self.role("developer", suffix=f":{index}", references=[explanation],
                           candidates=use, preferred=preferred, previous_review=previous_review)
        priorities = {row["label"]: {key: row[key] for key in COMPONENTS} for row in scores["priorities"]}
        priorities = self.release_priorities(index, priorities)
        bindings = {row["label"]: self.protocol(row, explanation) for row in use}
        tree = self.once(f"{label}-tree", "search.register_tree", weights={key: 1 for key in COMPONENTS},
                         cost_weight=0.01, budget=budget, cost_unit="enqueued_attempt", max_nodes=len(use),
                         max_depth=0, max_width=len(use), max_selections=1, max_retries=0)
        nodes = {}
        for candidate in use:
            key = candidate["label"]
            binding = bindings[key]
            nodes[self.once(f"{label}-node-{key}", "search.add_node", tree=tree, protocol=binding["protocol"],
                            action="discriminate" if phase == "exploratory" else "robustness",
                            components=priorities[key], estimated_cost=binding["roster"],
                            rationale=scores["reason"])] = key
        selection = self.once(f"{label}-selection", "search.select_next", tree=tree)
        if selection["node"] is None:
            reason = "budget_exhausted" if selection["reason"] == "budget_exhausted" else "inconclusive"
            raise _Stop(reason, f"No admissible experiment in the {phase} frontier ({selection['reason']}).")
        chosen = nodes[selection["node"]]
        candidate = next(row for row in use if row["label"] == chosen)
        binding = bindings[chosen]
        protocol = Kernel._get(self.history, binding["protocol"], "protocol")["payload"]
        design = protocol["statistical_design"]
        summary = {key: protocol[key] for key in ("design", "analysis_plan", "metric", "stopping_rule")}
        summary["statistical_design"] = {key: value for key, value in design.items() if key != "data_splits"}
        summary["declared_splits"] = [{key: row[key] for key in ("id", "role", "exposure_policy")}
                                      for row in design["data_splits"]]
        test = self.role("tester", suffix=f":{index}", references=[binding["protocol"], selection["id"]],
                         candidates=[candidate], protocol=summary,
                         test_scope="Check the frozen protocol and stated scope. No row data are supplied.")
        if test["decision"] != "run":
            self.once(f"{label}-cancel", "search.finish_selection", selection=selection["id"], status="cancelled",
                      actual_cost=0, reason=test["reason"])
            raise _Stop("inconclusive", "Tester requested a missing check: " + test["reason"][:1960])
        self.release_gate()
        event = Kernel._get(self.history, binding["binding"], "pack_binding")
        recipe = execution_fields(self.store, event)
        known_stage = _result(self.receipts, self.session, f"{label}-stage")
        saved = stage_status(self.store, known_stage["stage"]) if known_stage else None
        if saved is None or saved["status"] != "closed":
            self.check_roster_time(label, batch_id, selection["id"], binding["roster"], recipe["wall_seconds"])
        self.once(f"{label}-batch", "batch.plan", selection=selection["id"], executor=EXECUTOR, replicator=REANALYST,
                  **recipe)
        opened = self.once(f"{label}-stage", "stage.open", question=question, explanation_set=explanation,
                           pack_id=candidate["pack_id"], preregistration=binding["binding"],
                           attempt_ceiling=binding["roster"], criteria=[dict(kind="analysis_recorded")],
                           predecessor=predecessor)
        stage = stage_run(self.store, opened["stage"], analyst=Actor(ANALYST, "analyst"),
                          executor=EXECUTOR, replicator=REANALYST)
        self.snapshot()
        stage["recipe"] = chosen
        if stage["disposition"] == "blocked":
            raise _Stop("blocked", "Stage failed or has an unknown execution outcome. See its recorded attempts.")
        if stage["disposition"] == "stopped_budget":
            raise _Stop("budget_exhausted", "The required stage roster exceeded its budget.")
        if not stage["claim"]:
            raise _Stop("inconclusive", "The stage closed without an admissible analysis.")
        return stage

    def check_roster_time(self, label: str, batch: str | None, selection: str, roster: int,
                          job_seconds: int) -> None:
        pending = roster
        if batch:
            from .batch import batch_state

            slots = batch_state(self.store, batch)["slots"]
            for slot in slots:
                if slot["status"] == "unknown":
                    reconcile_job(self.store, slot["job"])
            slots = batch_state(self.store, batch)["slots"]
            if any(slot["status"] == "unknown" for slot in slots):
                return  # stage.run records the unresolved result without launching another attempt.
            pending = sum(slot["status"] in {"queued", "pending", "waiting_primary"} for slot in slots)
        seconds = remaining_seconds(self.view["steps"]["accept"], self.budget)
        if pending and (job_seconds > self.budget["operation_seconds"] or seconds < pending * job_seconds):
            if not batch:
                self.once(f"{label}-cancel", "search.finish_selection", selection=selection, status="cancelled",
                          actual_cost=0, reason="Remaining wall-time budget cannot cover the full roster.")
            raise _Stop("budget_exhausted", "Remaining time cannot cover the pending roster's pinned job limits.")

    def release_priorities(self, index: int, priorities: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        if self.contract["release"] is None:
            return priorities
        pin = _read(self.store, self.contract["release"])
        if pin["backend"] is None or not (pin["patch"] or "orchestrator" in pin["profile"]["role_instructions"]):
            return priorities
        key = f"policy:{index}"
        if self.step(key) is not None:
            return self.step(key)["payload"]["data"]["priorities"]
        from .evolution import DockerBackend

        backend = pin["backend"]
        worker = DockerBackend(backend["image"], worker=backend["worker"], timeout=min(backend["timeout"], self.seconds()))
        output = worker.invoke("policy", {"priorities": priorities}, pin)
        require(set(output) == {"priorities"}, "local release emitted fields outside the policy interface")
        self.record(key, "policy", dict(developer=self.step(f"task:developer:{index}")["id"],
                    release=self.contract["release"], priorities=output.get("priorities")))
        return self.step(key)["payload"]["data"]["priorities"]

    def review(self, stage: dict[str, Any], remaining: list[dict[str, Any]]) -> dict[str, Any]:
        gate = Kernel(self.store, Actor("research-observer", "observer"))._gate(self.history, stage["claim"])
        if not gate["passed"]:
            raise _Stop("inconclusive", "Mechanical admission does not support review of this claim.")
        claim = Kernel._get(self.history, stage["claim"], "claim")
        evidence = {row["id"]: row for row in self.history if row["kind"] == "run" and row["id"] in claim["payload"]["evidence"]}
        results = [row for row in self.history if row["kind"] == "result" and row["payload"]["run"] in evidence]
        reports = []
        for row in results:
            data = row["payload"].get("outputs", {}).get("metrics")
            reports.append(dict(id=row["id"], status=row["payload"]["status"], metric_digest=data,
                                metrics=_read(self.store, data) if data else None))
        seen = [stage["close"], stage["claim"], stage["protocol"], *[row["id"] for row in reports]]
        key, _ = review_key(self.store, self.history, stage["stage"], stage["claim"])
        return self.role("reviewer", suffix=key.removeprefix("reviewer"), references=seen,
                         candidates=remaining,
                         basis=gate["basis_hash"], result={key: stage[key] for key in (
                             "close", "claim", "protocol", "outcome", "inference_mode")}, observations=reports,
                         available_phases=sorted({row["phase"] for row in remaining}))

    def proceed(self) -> None:
        self.release_gate()
        self.literature()
        question, explanation, _, critic = self.planning()
        candidates = self.contract["candidates"][:]
        phase = "exploratory" if any(row["phase"] == "exploratory" for row in candidates) else "confirmatory"
        predecessor = None
        previous_review = None
        for index in range(self.budget["stages"]):
            stage = self.stage(index, phase, candidates, question, explanation, critic["preferred"], predecessor,
                               previous_review)
            candidates = [row for row in candidates if row["label"] != stage["recipe"]]
            predecessor = stage["stage"]
            following = self.step(f"task-intent:developer:{index + 1}")
            if following:
                context = _read(self.store, following["payload"]["data"]["context"])
                phase = context["candidates"][0]["phase"]
                previous_review = context["previous_review"]
                continue
            decision = self.review(stage, candidates)
            if decision["decision"] == "finish":
                self.refresh_reviews()
                return
            if decision["decision"] == "unresolved":
                raise _Stop("inconclusive", decision["reason"])
            phase = "confirmatory" if decision["decision"] == "confirm" else "exploratory"
            previous_review = decision
            if not any(row["phase"] == phase for row in candidates):
                raise _Stop("inconclusive", "Reviewer requested an unavailable check. " + decision["reason"][:1980])
        raise _Stop("budget_exhausted", "Stage ceiling reached with a requested check outstanding.")

    def refresh_reviews(self) -> None:
        for row in _stage_closes(self.history, self.receipts, self.session):
            stage = row["payload"]
            key, _ = review_key(self.store, self.history, stage["stage"], stage["claim"])
            if self.step(f"task:{key}") is None:
                decision = self.review(dict(stage, close=row["id"]), [])
                if decision["decision"] != "finish":
                    raise _Stop("inconclusive", "A refreshed review requests more evidence. " + decision["reason"][:1950])

    def finish(self, reason: str, detail: str) -> None:
        self.snapshot()
        closed = _stage_closes(self.history, self.receipts, self.session)
        claims, bases = [], {}
        for row in closed:
            claim = row["payload"]["claim"]
            if claim:
                info = Kernel(self.store, Actor("research-writer", "writer")).next_action(claim)
                if info["action"] == "paper_candidate":
                    claims.append(claim)
                    bases[claim] = info["basis_hash"]
        paper = None
        if claims:
            paper = _once(self.store, self.session, "paper", "paper.build", dict(
                title=self.opened["payload"]["hypothesis"][:200], claims=claims, expected_bases=bases),
                actor="research-writer", role="writer")
        self.record("finish", "finish", dict(reason=reason, detail=detail, paper=paper,
                                             stages=[dict(id=row["id"], hash=row["hash"]) for row in closed]))


def run_research(store: Store, session: str, *, accept: str | None = None, author: str = "author",
                 fetch: Any = None) -> dict[str, Any]:
    with _exclusive_run(store.root):
        history, receipts = store.snapshot()
        research_index(store, history, receipts)
        view = session_view(history, session)
        accepted = view["steps"].get("accept")
        if not accepted:
            require(accept is not None, "research draft must be accepted with its current revision")
            _record(store, session, "accept", "accept", dict(revision=accept), author=author)
        elif accept is not None:
            require(accepted["payload"]["data"]["revision"] == accept, "research already accepted another revision")
        if "finish" in view["steps"]:
            return _closed_status(store, session)
        runner = _Run(store, session, fetch=fetch)
        try:
            runner.proceed()
        except _AwaitCompletion as exc:
            if remaining_seconds(runner.view["steps"]["accept"], runner.budget) == 0:
                runner.finish("blocked", str(exc)[:2048])
            else:
                return dict(research_status(store, session), status="awaiting_completion", reason=str(exc))
        except _Stop as exc:
            runner.finish(exc.reason, exc.detail)
        except ConflictError:
            raise
        except ValueError as exc:
            runner.finish("blocked", (str(exc) or type(exc).__name__)[:2048])
        else:
            runner.finish("planned_checks_finished", "The current review requested no further in-scope checks.")
        return _closed_status(store, session)


def _closed_status(store: Store, session: str) -> dict[str, Any]:
    status = research_status(store, session)
    contract = _read(store, status["contract"])
    if contract["release_root"] is None:
        return status
    from .evolution import EvolutionChannel

    signals = []
    if not (Path(contract["release_root"]) / "state.sqlite3").is_file():
        return dict(status, evolution_status="release_channel_unavailable")
    with EvolutionChannel(contract["release_root"]) as channel:
        if channel.pin()["release"] != status["release"]:
            return status
        history = store.events()
        view = session_view(history, session)
        disposition = status["disposition"]
        for stage in disposition["stages"]:
            closed = Kernel._get(history, stage["id"], "stage_close")
            if closed["payload"]["disposition"] == "blocked":
                opened = Kernel._get(history, closed["payload"]["stage"], "stage_open")
                signals.append(("verified_failure", f"stage_blocked:{opened['payload']['pack_id']}", closed["hash"]))
        for row in view["rows"]:
            p = row["payload"]
            if p["step"] == "task" and p["data"]["result"]["status"] == "invalid":
                role = p["key"].split(":")[1]
                signals.append(("verified_failure", f"invalid_role_answer:{role}", row["hash"]))
        if disposition["reason"] == "budget_exhausted" and status["stage_results"]:
            signals.append(("measured_resource_loss", "unsettled_check_at_budget_limit", view["steps"]["finish"]["hash"]))
        if signals:
            status["evolution"] = [channel.record_signal(kind=kind, key=key, source=source,
                                                        expected_release=status["release"])
                                   for kind, key, source in signals]
    return status
