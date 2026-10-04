"""Snapshot-consistent evidence exports and internally reviewed paper scaffolds."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .claim_context import resolve_context
from .kernel import Actor, Kernel, require
from .literature import LITERATURE_KINDS, assert_current_support, literature_artifacts, locator_status, paper_literature
from .planning import planning_context, validate_planning
from .store import Store, canonical


FIXTURE_NOTICE = (
    "Synthetic fixture; no LLM/API calls or independent AI agents. "
    "Different fixture actor IDs and separately executed implementations exercise "
    "the kernel contract. Reanalysis uses the same observations, not new data. "
    "Mechanical checks do not assess scientific validity."
)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _roster_by_protocol(history: list[dict[str, Any]]) -> dict[str, str]:
    """Pinned roster_semantics by protocol. Absent on histories that have no pack binding."""
    found: dict[str, str] = {}
    for event in history:
        if event["kind"] != "pack_binding":
            continue
        protocol = event["payload"].get("protocol")
        semantics = event["payload"].get("roster_semantics")
        if isinstance(protocol, str) and isinstance(semantics, str):
            found[protocol] = semantics
    return found


def _roster_heading(history: list[dict[str, Any]], protocols: list[str]) -> str | None:
    """The column label when every listed protocol shares one pinned roster_semantics."""
    if not protocols:
        return None
    labels = _roster_by_protocol(history)
    tokens = [labels.get(protocol) for protocol in protocols]
    if any(token is None for token in tokens) or len(set(tokens)) != 1:
        return None
    return tokens[0]


def _atomic_text(path: Path, value: str) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _summary(store: Store, history: list[dict[str, Any]]) -> dict[str, Any]:
    kernel = Kernel(store, Actor("read-only-inspector", "observer"))
    claims = [{"id": e["id"], "statement": e["payload"]["statement"],
               "gate": kernel._gate(history, e["id"]),
               "next_action": kernel._next_action(history, e["id"])}
              for e in history if e["kind"] == "claim"]
    synthetic = any(e["kind"] == "protocol" and e["payload"]["scope"].get("mode") == "synthetic_demo"
                    for e in history)
    result: dict[str, Any] = dict(root=str(store.root), synthetic_demo=synthetic,
        event_count=len(history), counts=dict(Counter(e["kind"] for e in history)),
        last_event_hash=history[-1]["hash"] if history else "0" * 64, claims=claims)
    if synthetic:
        result["notice"] = FIXTURE_NOTICE
    if len(claims) == 1:
        result.update(claim=claims[0]["id"], next_action=claims[0]["next_action"])
    decisions = {claim["id"]: claim["next_action"] for claim in claims}
    papers = [dict(id=e["id"], status=paper_status(store, history, e, decisions))
              for e in history if e["kind"] == "paper"]
    if papers:
        result["papers"] = papers
    return result


def paper_status(store: Store, history: list[dict[str, Any]], paper: dict[str, Any],
                 decisions: dict[str, dict[str, Any]]) -> str:
    """A recorded draft stays in history; current rules decide what it is now (ADR 0018 §4.4).

    ``current``: eligible now; ``historical``: eligible on its own prefix, not now;
    ``not_eligible_under_current_rules``: not eligible even on its own prefix.
    """
    bases = paper["payload"]["reviewed_bases"]

    def eligible(rows: dict[str, dict[str, Any]]) -> bool:
        return all(rows[id]["action"] == "paper_candidate" and rows[id]["basis_hash"] == basis
                   for id, basis in bases.items())

    if eligible(decisions):
        return "current"
    prefix = history[:next(index for index, event in enumerate(history) if event["id"] == paper["id"])]
    reader = Kernel(store, Actor("paper-status-reader", "observer"))
    return ("historical" if eligible({id: reader._next_action(prefix, id) for id in bases})
            else "not_eligible_under_current_rules")


def inspect_store(store: Store) -> dict[str, Any]:
    return _summary(store, store.events())


def artifact_inventory(store: Store, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys: set[str] = set()
    for event in history:
        p = event["payload"]
        if event["kind"].startswith("agent_"):
            from .agents import agent_artifacts
            keys.update(agent_artifacts(store, event))
        if event["kind"] in {"batch_plan", "batch_slot", "batch_settlement"}:
            from .batch import batch_artifacts
            keys.update(batch_artifacts(store, event))
        if event["kind"] == "batch_analysis":
            keys.update((p["proposal_digest"], p["adapter_source_digest"]))
        if event["kind"] == "domain_binding":
            from .domain_binding import binding_artifacts
            keys.update(binding_artifacts(event))
        if event["kind"] in {"pack_binding", "pack_analysis"}:
            from .domain_packs import pack_artifacts
            keys.update(pack_artifacts(store, event))
        if event["kind"].startswith("execution_"):
            from .execution import execution_artifacts
            keys.update(execution_artifacts(store, event))
        if event["kind"] == "replan_followup":
            from .followup import followup_artifacts
            keys.update(followup_artifacts(event))
        if event["kind"] == "review_assignment":
            keys.add(p["bundle"])
        if event["kind"] == "review_dispatch":
            keys.add(p["request"])
        if event["kind"] == "review_response" and p["response"] is not None:
            keys.add(p["response"])
        if event["kind"] == "protocol":
            keys.update(p[key] for key in ("implementation", "environment", "data"))
            if p.get("statistical_design") is not None:
                keys.update(split["digest"] for split in p["statistical_design"]["data_splits"])
                keys.update(p["seen_data"])
        elif event["kind"] == "data_exposure":
            keys.add(p["data"])
        elif event["kind"] == "run":
            keys.update(p[key] for key in ("implementation", "environment"))
        elif event["kind"] == "result":
            keys.update(p["outputs"].values())
        elif event["kind"] == "paper":
            keys.update((p["manuscript"], p["bundle"]))
        elif event["kind"] in LITERATURE_KINDS:
            keys.update(literature_artifacts(event))
        elif event["kind"] == "afterlife_snapshot":
            keys.add(p["snapshot"])
            snapshot = json.loads(store.read(p["snapshot"]))
            require(snapshot.get("schema") == "afterlife-historical-snapshot-v1",
                    "unsupported historical artifact inventory")
            require(isinstance(snapshot.get("blob_digests"), list)
                    and all(isinstance(key, str) for key in snapshot["blob_digests"]),
                    "invalid historical artifact inventory")
            keys.update(snapshot["blob_digests"])
    return [dict(sha256=key, path=f"artifacts/sha256/{key}", bytes=len(store.read(key)))
            for key in sorted(keys)]


def review_bundle(store: Store, history: list[dict[str, Any]]) -> dict[str, Any]:
    validate_planning(history)
    from .replanning import _index as replanning_index
    from .batch_analysis import _index as analysis_index
    from .domain_binding import _index as binding_index
    from .review_assignment import _index as assignment_index
    from .reviewer_controller import _index as delivery_index
    from .review_submission import _index as submission_index
    analysis_index(store, history)
    binding_index(store, history)
    packs = any(event["kind"] in {"pack_binding", "pack_analysis"} for event in history)
    if packs:
        from .domain_packs import pack_analyses, pack_bindings
        pack_bindings(store, history)
        pack_analyses(store, history)
    assignment_index(store, history)
    delivery_index(store, history)
    submission_index(store, history)
    from .followup import _index as followup_index
    replanning_index(store, history)
    followup_index(store, history)
    from .resolution import resolution_states
    resolutions = resolution_states(store, history)
    from .agents import agent_context
    agent_context(store, history, set())
    from .execution import execution_context
    execution_context(store, history, set())
    from .batch import batch_summaries
    batches = batch_summaries(store, history)
    summary = _summary(store, history)
    if packs:
        # Pack sections appear only in pack histories.
        return dict(_bundle(store, history, summary, batches, resolutions),
                    pack_bindings=[event for event in history if event["kind"] == "pack_binding"],
                    pack_analyses=[event for event in history if event["kind"] == "pack_analysis"])
    return _bundle(store, history, summary, batches, resolutions)


def admission_record(store: Store, history: list[dict[str, Any]], claim: str) -> dict[str, Any]:
    """Which reviews count for claim now and why the others do not (ADR 0018 §4.4)."""
    from .review_admission import admission
    projection = admission(store, history)
    submissions = projection._submitted()
    by_id = {event["id"]: event for event in history}
    basis = Kernel(store, Actor("admission-reporter", "observer"))._gate(history, claim)["basis_hash"]
    approvals = []
    for event in history:
        p = event["payload"]
        if event["kind"] != "review" or p["claim"] != claim or p["verdict"] != "approve":
            continue
        submission = submissions.get(event["id"])
        sp = {} if submission is None else submission["payload"]
        assignment = by_id.get(sp.get("assignment", ""))
        defect = projection.approval_defect(event)
        if defect is None and p["basis_hash"] != basis:
            defect = "approval of an earlier evidence basis"
        approvals.append(dict(review=event["id"], reviewer=event["actor"], basis_hash=p["basis_hash"],
                              assignment=sp.get("assignment"), dispatch=sp.get("dispatch"),
                              submission=None if submission is None else submission["id"],
                              policy=None if assignment is None else assignment["payload"]["policy"],
                              rationale=p["rationale"], counted=defect is None, defect=defect))
    family = set(projection.family(claim))
    return dict(
        approvals=approvals,
        vetoes=[dict(opinion=opinion["id"], claim=opinion["payload"]["claim"],
                     reviewer=opinion["actor"]) for opinion in projection.vetoes(claim)],
        withdrawals=[dict(opinion=row["opinion"], submission=event["id"])
                     for event in projection.submissions() if event["payload"]["claim"] == claim
                     for row in event["payload"].get("withdrawals", [])],
        resolutions=[dict(resolution=record["resolution"]["id"],
                          obligation=record["resolution"]["payload"]["obligation"],
                          claim=record["resolution"]["payload"]["claim"], status=record["status"])
                     for record in projection.resolutions()
                     if record["resolution"]["payload"]["claim"] in family])


def _bundle(store: Store, history: list[dict[str, Any]], summary: dict[str, Any],
            batches: list[dict[str, Any]], resolutions: dict[str, Any]) -> dict[str, Any]:
    from .review_admission import admission, attempt_ledger
    claims = [event["id"] for event in history if event["kind"] == "claim"]
    projection = admission(store, history)
    result = dict(bundle_version=2, summary=summary, events=history,
                claim_families={id: dict(members=list(projection.family(id)),
                                         attempt_ledger=attempt_ledger(store, history, id))
                                for id in claims},
                review_admission={id: admission_record(store, history, id) for id in claims},
                execution_batches=batches,
                batch_analyses=[event for event in history if event["kind"] == "batch_analysis"],
                domain_bindings=[event for event in history if event["kind"] == "domain_binding"],
                artifacts=artifact_inventory(store, history),
                claim_relations=[event for event in history if event["kind"] == "claim_link"],
                review_obligations=[event for event in history if event["kind"] == "review_obligation"],
                review_obligation_resolutions=[event for event in history
                                               if event["kind"] == "review_obligation_resolution"],
                obligation_resolution_status={
                    event["id"]: resolutions.get(event["id"], {}).get("status", "open")
                    for event in history if event["kind"] == "review_obligation"},
                replan_followups=[event for event in history if event["kind"] == "replan_followup"],
                research_questions=[event for event in history if event["kind"] == "research_question"],
                explanation_sets=[event for event in history if event["kind"] == "explanation_set"],
                delivery_restore="events_only; command receipts require a separate database backup",
                scientific_review="not performed by export", snapshot_hash=summary["last_event_hash"])
    literature = [event for event in history if event["kind"] in LITERATURE_KINDS]
    if literature:
        # Absent on histories that have no literature records, so their bundle hash stays.
        result["literature"] = literature
    return result


def _run_table(store: Store, history: list[dict[str, Any]], runs: list[dict[str, Any]]) -> list[str]:
    heading = _roster_heading(history, [run["payload"]["protocol"] for run in runs])
    column = heading if heading is not None else "Seed"
    rows = []
    if heading is not None:
        rows.append(f"Roster column label is pinned roster_semantics `{heading}`.")
        rows.append("")
    rows.extend([f"| Run | Kind | {column} | Primary metric | Value | Raw data | Implementation |",
                 "| --- | --- | --- | --- | --- | --- | --- |"])
    jobs = {e["payload"]["run"]: e for e in history if e["kind"] == "execution_job"}
    for run in runs:
        p = run["payload"]
        plan = Kernel._get(history, p["protocol"], "protocol")["payload"]
        result = Kernel._result(history, run["id"])
        status = result["payload"]["status"] if result else "running"
        if result is None and run["id"] in jobs:
            status = "unknown" if any(e["kind"] == "execution_dispatch" and e["payload"]["job"] == jobs[run["id"]]["id"]
                                      for e in history) else "queued"
        if status != "completed":
            # ADR 0018 §4.4: failed and cancelled attempts keep their recorded metric and reason.
            outputs = result["payload"]["outputs"] if result else {}
            from .review_admission import _finite_metric
            value = _finite_metric(store, outputs.get("metrics"), plan["metric"])
            reason = result["payload"].get("reason", "") if result else ""
            label = f"{status}: {_cell(reason[:512])}" if reason else status
            raw = f"[raw](artifacts/sha256/{outputs['raw_data']})" if "raw_data" in outputs else "—"
            rows.append(f"| {run['id']} | {label} | {p['seed']} | {_cell(plan['metric'])} | "
                        f"{'—' if value is None else _cell(value)} | {raw} | — |")
            continue
        outputs = result["payload"]["outputs"]
        metrics = json.loads(store.read(outputs["metrics"]))
        kind = "reanalysis of same data" if p["replicate_of"] else "primary"
        rows.append(f"| {run['id']} | {kind} | {p['seed']} | {_cell(plan['metric'])} | "
                    f"{_cell(metrics[plan['metric']])} | "
                    f"[raw](artifacts/sha256/{outputs['raw_data']}) | "
                    f"[source](artifacts/sha256/{p['implementation']}) |")
    selected = [jobs[run["id"]] for run in runs if run["id"] in jobs]
    if selected:
        rows.extend(["", "Execution provenance: trusted local Python, without filesystem/network sandbox. "
                     "Separate processes do not prove independent scientific reasoning.", ""])
        for job in selected:
            terminal = next((e for e in history if e["kind"] == "execution_finalized" and e["payload"]["job"] == job["id"]), None)
            manifest = (f"[completion](artifacts/sha256/{terminal['payload']['manifest']})"
                        if terminal else "no verified completion")
            rows.append(f"- `{job['payload']['run']}`: `{job['payload']['mode']}`; "
                        f"[frozen specification](artifacts/sha256/{job['payload']['specification']}); {manifest}.")
    return rows


def _relation_table(links: list[dict[str, Any]]) -> list[str]:
    if not links:
        return []
    rows = ["## Recorded claim relations", "",
            "Relations are preserved proposals and context, not automatically established scientific truth.", "",
            "| Link | Source | Relation | Target | Recorded rationale |",
            "| --- | --- | --- | --- | --- |"]
    for link in links:
        p = link["payload"]
        rows.append(f"| {link['id']} | {p['source']} | {p['relation']} | {p['target']} | {_cell(p['rationale'])} |")
    return [*rows, ""]


def _planning_table(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return []
    lines = ["## Recorded research planning", "",
             "Versioned declarations; constraints and stopping criteria are not automatically executed.", ""]
    for event in records:
        p = event["payload"]
        if event["kind"] == "research_question":
            lines.extend([f"### Question `{event['id']}`", "", _cell(p["statement"]), "",
                f"Study: `{_cell(p['study_id'])}`. Objective: {_cell(p['objective'])}", "",
                f"Scope: `{_cell(json.dumps(p['scope'], ensure_ascii=False))}`.", "",
                *(f"- Constraint: {_cell(item)}" for item in p["constraints"]),
                *(f"- Stopping criterion: {_cell(item)}" for item in p["stopping_criteria"]), ""])
        elif event["kind"] == "explanation_set":
            lines.extend([f"### Explanation set `{event['id']}`", "",
                f"Question revision: `{p['question']}`. Candidates: {', '.join(p['hypotheses'])}.", "",
                f"Comparison plan: {_cell(p['comparison_plan'])}", "",
                *(f"- Excluded candidate `{id}`: {_cell(reason)}" for id, reason in p["excluded_reasons"].items()), ""])
        elif event["kind"] == "hypothesis":
            lines.extend([f"### Hypothesis `{event['id']}`", "", _cell(p["statement"]), "",
                          f"Prediction: {_cell(p['prediction'])}", "",
                          f"Falsifier: {_cell(p['falsifier'])}", ""])
            continue
        else:
            continue
        if p["parent"] is not None:
            lines.extend([f"Prior revision: `{p['parent']}`. Revision reason: {_cell(p['revision_reason'])}", ""])
    return lines


def _paper_followup_lineage(history: list[dict[str, Any]], claim: str,
                           records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trace reviewer findings through protocol ancestry at the paper snapshot.

    Claim links are optional for a follow-up. The frozen protocol parent chain,
    rather than a statement match, identifies the relevant negative review.
    """
    by_id = {event["id"]: event for event in history}
    protocol = Kernel._get(history, claim, "claim")["payload"]["protocol"]
    ancestors: list[str] = []
    while protocol is not None:
        require(protocol not in ancestors, "protocol parent cycle in paper lineage")
        ancestors.append(protocol)
        protocol = Kernel._get(history, protocol, "protocol")["payload"]["parent"]
    followups = {event["payload"]["protocol"]: event for event in history
                 if event["kind"] == "replan_followup"}
    lineage = []
    for protocol in reversed(ancestors):
        followup = followups.get(protocol)
        if followup is None:
            continue
        fp = followup["payload"]
        obligation = Kernel._get(history, fp["obligation"], "review_obligation")
        op = obligation["payload"]
        source_review = Kernel._get(history, op["review"], "review")
        source_claim = Kernel._get(history, op["claim"], "claim")
        candidates = [record for record in records
                      if record["resolution"]["payload"]["obligation"] == obligation["id"]]
        state = next((record for record in reversed(candidates)
                      if record["resolution"]["payload"]["claim"] == claim),
                     candidates[-1] if candidates else None)
        status = state["status"] if state is not None else "open"
        # PaperBuilder has already checked next_action. Keep this projection
        # fail-closed if its coverage ever diverges from the paper gate.
        require(status == "reviewer_satisfied",
                f"unresolved follow-up obligation in paper lineage: {obligation['id']}")
        decision = state["resolution"]
        dp = decision["payload"]
        require(dp["followup"] == followup["id"]
                and dp["obligation"] == obligation["id"],
                "paper lineage resolution differs from its frozen follow-up")
        # ADR 0018: a resolution covers only the claim its reviewer evaluated.
        require(dp["claim"] == claim,
                f"paper lineage resolution was granted for another claim: {obligation['id']}")
        citations = []
        for ref in dp["evidence_refs"]:
            event = by_id[ref["id"]]
            require(event["hash"] == ref["hash"],
                    "paper lineage citation differs from its recorded event revision")
            citation = dict(id=event["id"], hash=event["hash"], kind=event["kind"])
            if event["kind"] == "result":
                citation["run"] = event["payload"]["run"]
            elif event["kind"] == "claim":
                citation["protocol"] = event["payload"]["protocol"]
            citations.append(citation)
        lineage.append(dict(
            source_claim=dict(id=source_claim["id"], hash=source_claim["hash"],
                              protocol=source_claim["payload"]["protocol"],
                              statement=source_claim["payload"]["statement"]),
            source_review=dict(id=source_review["id"], hash=source_review["hash"],
                               actor=source_review["actor"],
                               verdict=source_review["payload"]["verdict"],
                               rationale=source_review["payload"]["rationale"],
                               basis_hash=source_review["payload"]["basis_hash"]),
            obligation=dict(id=obligation["id"], hash=obligation["hash"],
                            kind=op["kind"], action=op["action"],
                            closure_criterion=op["closure_criterion"],
                            evidence_refs=op["evidence_refs"]),
            followup=dict(id=followup["id"], hash=followup["hash"],
                          protocol=fp["protocol"], protocol_hash=fp["protocol_hash"],
                          experiment_node=fp["experiment_node"],
                          experiment_node_hash=fp["experiment_node_hash"]),
            resolution=dict(id=decision["id"], hash=decision["hash"],
                            review=dp["review"], review_hash=dp["review_hash"],
                            claim=dp["claim"], basis_hash=dp["basis_hash"],
                            rationale=dp["resolution_rationale"],
                            recorded_disposition=dp["disposition"],
                            effective_status=status, evidence_refs=citations)))
    return lineage


def _followup_manuscript(claim: str, lineage: list[dict[str, Any]]) -> list[str]:
    if not lineage:
        return []
    lines = [f"### Review-driven follow-up lineage for `{claim}`", "",
             "Recorded source findings and the original reviewer's evidence-bound opinions "
             "for this claim's protocol ancestry. `reviewer_satisfied` is effective at "
             "the source snapshot; it does not establish independent scientific validity. "
             "This section traces bound follow-ups; sibling findings remain in the full review "
             "bundle and continue to gate paper eligibility.", ""]
    for step in lineage:
        source, review = step["source_claim"], step["source_review"]
        obligation, followup, decision = (step["obligation"], step["followup"], step["resolution"])
        lines.extend([
            f"- Source claim `{source['id']}` (event `{source['hash']}`): {_cell(source['statement'])}",
            f"  - Negative review `{review['id']}` (event `{review['hash']}`), "
            f"reviewer `{_cell(review['actor'])}`, verdict `{review['verdict']}`: "
            f"{_cell(review['rationale'])}",
            f"  - Obligation `{obligation['id']}` (event `{obligation['hash']}`), "
            f"`{obligation['kind']}`: {_cell(obligation['action'])} "
            f"Closure criterion: {_cell(obligation['closure_criterion'])}",
            "  - Original finding citations: " + "; ".join(
                f"`{ref['id']}` (event `{ref['hash']}`)"
                for ref in obligation["evidence_refs"]),
            f"  - Frozen follow-up `{followup['id']}` (event `{followup['hash']}`), "
            f"protocol `{followup['protocol']}` (event `{followup['protocol_hash']}`), "
            f"node `{followup['experiment_node']}` (event `{followup['experiment_node_hash']}`).",
            f"  - Reviewer resolution `{decision['id']}` (event `{decision['hash']}`) "
            f"for bounded claim `{decision['claim']}`: recorded `{decision['recorded_disposition']}`; "
            f"effective `{decision['effective_status']}`. {_cell(decision['rationale'])}",
            "  - Cited new evidence: " + "; ".join(
                f"`{ref['id']}` ({ref['kind']}, event `{ref['hash']}`)"
                for ref in decision["evidence_refs"]), ""])
    return lines


def _ledger_manuscript(ledger: dict[str, Any], history: list[dict[str, Any]] | None = None) -> list[str]:
    family = [row for row in ledger["attempts"] if row["tier"] == "family"]
    heading = None if history is None else _roster_heading(
        history, [row["protocol"] for row in family])
    column = heading if heading is not None else "Seed"
    lines = ["### Claim family and attempt ledger", "",
             "Every recorded attempt on the protocols of this claim's family, including failed, "
             "unknown and queued ones. Reanalyses use the same data; none is a new-data replication.", ""]
    if heading is not None:
        lines.extend([f"Roster column label is pinned roster_semantics `{heading}`.", ""])
    repetitions = sorted({row["roster_repetition"] for row in family
                          if row.get("roster_repetition") not in {None, "undeclared"}})
    if repetitions:
        lines.extend(["Kernel roster repetition: " + ", ".join(f"`{item}`" for item in repetitions) + ".", ""])
    lines.extend(["Family protocols: " + ", ".join(f"`{row['id']}` ({row['edge']})"
                                                   for row in ledger["family_protocols"]) + ".", "",
                  f"| Run | Protocol | {column} | Kind | Status | Primary metric | Value | Outputs |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"])
    for row in ledger["attempts"]:
        if row["tier"] != "family":
            continue
        kind = (f"primary attempt {row['primary_attempt']}" if row["kind"] == "primary"
                else f"reanalysis of {row['replicate_of']}")
        status = f"{row['status']}: {_cell(row['reason'])}" if row["reason"] else row["status"]
        value = "—" if row["metric_value"] is None else _cell(row["metric_value"])
        origin = row["outputs_origin"] + (", metrics artifact shared with original"
                                          if row.get("metrics_artifact_shared_with_original") else "")
        lines.append(f"| {row['run']} | {row['protocol']} | {row['seed']} | {kind} | {status} | "
                     f"{_cell(row['primary_metric'])} | {value} | {origin} |")
    related = [row for row in ledger["attempts"] if row["tier"] == "related_registration"]
    if ledger["related_registrations"]:
        lines.extend(["", "### Related registrations", "",
                      "Protocols sharing a hypothesis without a protocol edge; disclosed, not evidence.", ""])
        lines.extend(f"- Protocol `{row['id']}`: " + (", ".join(
            f"`{attempt['run']}` seed {attempt['seed']} {attempt['status']}"
            + ("" if attempt["metric_value"] is None else f" ({_cell(attempt['metric_value'])})")
            for attempt in related if attempt["protocol"] == row["id"]) or "no runs") + "."
            for row in ledger["related_registrations"])
    return [*lines, ""]


def _admission_manuscript(record: dict[str, Any]) -> list[str]:
    lines = ["### Review admission", "",
             "Counted approvals come from a verified assignment, delivery and review.submit chain. "
             "Reviewer IDs are caller-declared; admission is not scientific validation.", ""]
    for row in record["approvals"]:
        if row["counted"]:
            lines.append(f"- Counted approval `{row['review']}` by `{_cell(row['reviewer'])}`: assignment "
                         f"`{row['assignment']}`, dispatch `{row['dispatch']}`, submission "
                         f"`{row['submission']}`, policy `{row['policy']}`. "
                         f"Recorded rationale: {_cell(row['rationale'])}. "
                         "Counting this record does not assess scientific validity.")
        else:
            lines.append(f"- Not counted: review `{row['review']}` by `{_cell(row['reviewer'])}`: "
                         f"{_cell(row['defect'])}.")
    lines.extend(f"- Open veto `{row['opinion']}` on `{row['claim']}` by `{_cell(row['reviewer'])}`."
                 for row in record["vetoes"])
    lines.extend(f"- Withdrawal of `{row['opinion']}` in submission `{row['submission']}`."
                 for row in record["withdrawals"])
    lines.extend(f"- Resolution `{row['resolution']}` of `{row['obligation']}` for claim "
                 f"`{row['claim']}`: `{row['status']}`." for row in record["resolutions"])
    return [*lines, ""]


def export_store(store: Store) -> dict[str, str]:
    # All files refer to precisely this verified history, even if another writer
    # appends while the export is materialized. Each file is atomically replaced.
    history = store.events()
    bundle = review_bundle(store, history)
    summary = bundle["summary"]
    report = ["# Synthetic research fixture" if summary["synthetic_demo"] else "# Research state export", "",
              FIXTURE_NOTICE if summary["synthetic_demo"] else "Recorded evidence; export is not scientific approval.",
              "", f"Events: {len(history)}. Snapshot: `{summary['last_event_hash']}`.", ""]
    for claim in summary["claims"]:
        c = Kernel._get(history, claim["id"], "claim")["payload"]
        report.extend([f"## Claim {claim['id']}", "", c["statement"], "",
                       f"Mechanical gate: **{'passed' if claim['gate']['passed'] else 'failed'}**. "
                       "Scientific validity: **not_assessed**.", "",
                       f"Next action: **{claim['next_action']['action']}**.", ""])
        report.extend(f"- Gate failure: {failure}" for failure in claim["gate"]["failures"])
        report.extend(["", "Limitations:", "", *(f"- {item}" for item in c["limitations"]), ""])
    report.extend(_relation_table(bundle["claim_relations"]))
    if bundle["execution_batches"]:
        report.extend(["## Execution batches", "",
            "Technical execution alone does not establish a claim or scientific review.", "",
            "| Batch | Status | Enqueued / planned attempts | Next action |",
            "| --- | --- | --- | --- |"])
        report.extend(f"| {_cell(row['batch'])} | {_cell(row['status'])} | "
                      f"{row['enqueued_attempts']} / {row['plan']['reserved_cost']} | {_cell(row['next_action'])} |"
                      for row in bundle["execution_batches"])
        report.append("")
    if bundle["batch_analyses"]:
        report.extend(["## Batch analyses", "",
            "Adapter proposals are recorded interpretations. Scientific validity remains not_assessed; reviewer opinions and mechanical gates are separate records.", "",
            "| Batch | Claim | Adapter | Intended reviewer |",
            "| --- | --- | --- | --- |"])
        report.extend(f"| {_cell(row['payload']['batch'])} | {_cell(row['payload']['claim'])} | "
                      f"{_cell(row['payload']['adapter_id'])} | {_cell(row['payload']['reviewer_actor'])} |"
                      for row in bundle["batch_analyses"])
        report.append("")
    if bundle.get("pack_analyses"):
        report.extend(["## DomainPack analyses", "",
            "Pack reports are trusted local proposals under a kernel-computed strength ceiling. "
            "Scientific validity remains not_assessed; mechanical gates and reviews are separate records.", "",
            "| Batch | Claim | Pack | Code digest | Ceiling | Intended reviewer |",
            "| --- | --- | --- | --- | --- | --- |"])
        report.extend(f"| {_cell(row['payload']['batch'])} | {_cell(row['payload']['claim'])} | "
                      f"{_cell(row['payload']['pack_id'])} {_cell(row['payload']['pack_version'])} | "
                      f"`{row['payload']['pack_code_digest']}` | "
                      f"{_cell(row['payload']['ceiling']['max_inference_mode'])} / "
                      f"{_cell('/'.join(row['payload']['ceiling']['allowed_outcomes']))} | "
                      f"{_cell(row['payload']['reviewer_actor'])} |"
                      for row in bundle["pack_analyses"])
        report.append("")
        semantics = _roster_by_protocol(history)
        for row in bundle["pack_analyses"]:
            payload = row["payload"]
            replication = payload["replication"]
            independence = replication["independence"]
            missing = payload["ceiling"].get("not_supplied") or []
            report.append(
                f"- Analysis `{row['id']}`: roster_semantics "
                f"`{semantics.get(payload['protocol'], 'undeclared')}`; "
                f"replication mode `{replication['mode']}` on `{replication['data']}`; "
                f"roster repetition `{replication['roster_repetition']}`; "
                f"independence implementation `{independence['implementation']}`, "
                f"context `{independence['context']}`, data `{independence['data']}`, "
                f"actors `{independence['actors']}`.")
            report.append("- Statistical report not_supplied: " + (
                ", ".join(f"`{name}`" for name in missing) if missing else "none") + ".")
        report.append("")
    planning_ids = {record["id"] for event in history if event["kind"] == "protocol"
                    and "planning" in event["payload"]
                    for record in planning_context(history, event["payload"]["planning"])}
    report.extend(_planning_table([event for event in history if event["id"] in planning_ids
                                  or event["kind"] in {"research_question", "explanation_set"}]))
    report.extend(["## Recorded computations", "", *_run_table(
        store, history, [e for e in history if e["kind"] == "run"]), "",
        "Frozen protocols, runtime records and artifact hashes: [review-bundle.json](review-bundle.json).", ""])
    files = {"review-bundle.json": json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
             "report.md": "\n".join(report),
             "events.jsonl": "".join(canonical(event).decode() + "\n" for event in history)}
    for name, content in files.items():
        _atomic_text(store.root / name, content)
    return {name: str(store.root / name) for name in files}


def _literature_manuscript(history: list[dict[str, Any]], extra: dict[str, Any]) -> list[str]:
    """Show stored links. Absence of sources is not novelty and not support."""
    lines = ["## Recorded literature", "",
             "Missing literature is not novelty and is not support for a scientific claim. "
             "This scaffold stores links from a manuscript citation to a locator. "
             "It does not search, retrieve, extract text by a model, search for contradictions, "
             "or assess novelty.", ""]
    for citation_id in extra["citations"]:
        citation = Kernel._get(history, citation_id, "literature_citation")
        locator = Kernel._get(history, citation["payload"]["locator"], "literature_locator")
        source = Kernel._get(history, locator["payload"]["source"], "literature_source")
        status = locator_status(history, locator["id"])
        locator_payload, source_payload = locator["payload"], source["payload"]
        authors = "; ".join(source_payload["authors"])
        lines.append(
            f"- Citation `{citation_id}` points at locator `{locator['id']}` "
            f"({locator_payload['locator_kind']} `{_cell(locator_payload['locator'])}`) "
            f"of source `{source['id']}` \"{_cell(source_payload['title'])}\" "
            f"({source_payload['year']}). Recorded authors: {_cell(authors)}.")
        lines.append(f"  Locator verification status: `{status}`.")
        claims = [event for event in history if event["kind"] == "literature_claim"
                  and event["payload"].get("locator") == locator["id"]
                  and event["payload"].get("locator_hash") == locator["hash"]]
        if claims:
            for claim in claims:
                lines.append(
                    f"  Extracted statement `{claim['id']}` by "
                    f"`{_cell(claim['payload']['extraction_actor'])}`, not a scientific finding "
                    f"and not a novelty assessment: {_cell(claim['payload']['statement'])}")
        else:
            lines.append("  No extracted statement is recorded for this locator.")
        if citation_id in extra["support"]:
            check = Kernel._get(history, extra["support_checks"][citation_id], "literature_check")
            check_payload = check["payload"]
            lines.append(
                "  Used as a recorded support link because locator status is "
                "`verified_by_recorded_check`. "
                f"Check `{check['id']}` names locator bytes `{check_payload['locator_sha256']}` "
                f"and passage digest `{check_payload['passage_digest']}`.")
            if check_payload["checker_kind"] == "fixture":
                lines.append(
                    "  A fixture check is not a librarian's verification and not a scientific review.")
            else:
                lines.append(
                    "  This recorded check is a persisted statement by a caller-declared actor. "
                    "It is not a retrieval performed by the kernel and not a scientific review.")
            lines.append("  Scientific validity of the research claim remains `not_assessed`.")
        else:
            lines.append("  Not used as support for a scientific claim.")
            if status == "contradicted":
                lines.append("  A contradicted locator cannot support a claim.")
            elif status == "unverified":
                lines.append("  An unverified locator cannot support a claim.")
        lines.append("")
    return lines


class PaperBuilder:
    """Build a traceable internal draft, never an automatic venue submission."""

    def __init__(self, store: Store, actor: Actor):
        self.kernel = Kernel(store, actor)
        self.store = store

    def build(self, *, title: str, claims: list[str], expected_bases: dict[str, str],
              citations: list[str] | None = None, support: list[str] | None = None) -> str:
        history = self.store.events()
        require(isinstance(title, str) and title.strip(), "paper title required")
        require(bool(claims) and len(set(claims)) == len(claims), "paper requires unique claims")
        require(set(expected_bases) == set(claims), "paper requires a reviewed basis for every claim")
        decisions = {id: self.kernel._next_action(history, id) for id in claims}
        for id, decision in decisions.items():
            require(decision["action"] == "paper_candidate", f"claim not eligible for paper: {id}")
            require(decision["basis_hash"] == expected_bases[id], f"stale paper evidence: {id}")
            c = Kernel._get(history, id, "claim")["payload"]
            scopes = (c["scope"], Kernel._get(history, c["protocol"], "protocol")["payload"]["scope"])
            require(all(scope.get("mode") != "synthetic_demo" for scope in scopes),
                    f"synthetic demo claims never enter a paper: {id}")
        literature = paper_literature(history, citations, support)
        bundle = review_bundle(self.store, history)
        contexts = [resolve_context(history, id) for id in claims]
        context_claims = {id for context in contexts for id in context.claim_ids}
        context_links = {id for context in contexts for id in context.link_ids}
        # review_bundle has replay-checked these decisions and calculated their
        # effective status on precisely the same history snapshot.
        from .resolution import resolution_records
        records = resolution_records(self.store, history, replay=False)
        followup_lineage = {id: _paper_followup_lineage(history, id, records) for id in claims}
        bundle.update(selected_claims=claims, reviewed_bases=expected_bases,
                      selected_context=dict(claims=[e["id"] for e in history if e["id"] in context_claims],
                                            links=[e["id"] for e in history if e["id"] in context_links],
                                            followup_lineage=followup_lineage),
                      paper_status="internal evidence-linked scaffold; human release pending")
        lines = [f"# {_cell(title)}", "", "**Internal evidence-linked draft.** "
                 "This scaffold records reviewed claims and computations. It is not a submission-ready paper.", "",
                 "## Research record", "", f"Source snapshot: `{bundle['snapshot_hash']}`.", ""]
        for id in claims:
            c = Kernel._get(history, id, "claim")["payload"]
            p = Kernel._get(history, c["protocol"], "protocol")["payload"]
            if "planning" in p:
                lines.extend(_planning_table(planning_context(history, p["planning"])))
            lines.extend([f"## Result {id}", "", c["statement"], "",
                          f"Recorded outcome: `{c['outcome']}`. Scope: `{json.dumps(c['scope'], ensure_ascii=False)}`.",
                          "", "Scientific validity: `not_assessed`. A counted local approval is an admitted "
                          "record, not a scientific assessment.",
                          "", f"Protocol: `{c['protocol']}`. Review basis: `{expected_bases[id]}`.", ""])
            pack_rows = [event for event in history if event["kind"] == "pack_analysis"
                         and event["payload"].get("claim") == id]
            if pack_rows:
                replication = pack_rows[-1]["payload"]["replication"]
                independence = replication["independence"]
                semantics = _roster_by_protocol(history).get(c["protocol"], "undeclared")
                lines.extend([
                    f"Pinned roster_semantics: `{semantics}`. Kernel replication mode: "
                    f"`{replication['mode']}` on `{replication['data']}`; roster repetition "
                    f"`{replication['roster_repetition']}`. Independence of implementation "
                    f"`{independence['implementation']}`, context `{independence['context']}`, "
                    f"data `{independence['data']}`, actors `{independence['actors']}`.", ""])
            lines.extend(["### Registered methods", "", p["design"], "", p["analysis_plan"], "",
                          f"Stopping rule: {p['stopping_rule']}", "", "### Evidence", ""])
            runs = [e for e in history if e["kind"] == "run" and e["payload"]["protocol"] == c["protocol"]]
            lines.extend(_run_table(self.store, history, runs))
            lines.extend(["", *_followup_manuscript(id, followup_lineage[id])])
            ledger = bundle["claim_families"][id]["attempt_ledger"]
            lines.extend(["", *_ledger_manuscript(ledger, history),
                          *_admission_manuscript(bundle["review_admission"][id])])
            lines.extend(["", "### Limitations", "", *(f"- {item}" for item in c["limitations"]),
                          *(f"- Kernel disclosure: {item}" for item in ledger["disclosures"]), ""])
        links = [e for e in history if e["id"] in context_links]
        lines.extend(_relation_table(links))
        if links:
            findings = {id: self.kernel._context_findings(history, id) for id in claims}
            bundle["selected_context"]["review_findings"] = {
                id: dict(events=[event["id"] for event in records], open_reviews=sorted(pending))
                for id, (records, pending) in findings.items()}
            lines.extend(["## Competing and prior claims", "",
                          "These records remain evidence context. Inclusion does not promote them to current approved results.", ""])
            for id in bundle["selected_context"]["claims"]:
                if id in claims:
                    continue
                c = Kernel._get(history, id, "claim")["payload"]
                gate = self.kernel._gate_local(history, id)
                lines.extend([f"### Context claim {id}", "", c["statement"], "",
                              f"Recorded outcome: `{c['outcome']}`. Current local mechanical gate: "
                              f"`{'passed' if gate['passed'] else 'failed'}`; scientific validity: `not_assessed`.", "",
                              *(f"- Recorded limitation: {item}" for item in c["limitations"]), ""])
                lines.extend(_run_table(self.store, history, [e for e in history
                    if e["kind"] == "run" and e["payload"]["protocol"] == c["protocol"]]))
                lines.append("")
            lines.extend(["## Current relation assessments for selected claims", ""])
            for id in claims:
                latest = {e["actor"]: e for e in history if e["kind"] == "review"
                          and e["payload"]["claim"] == id
                          and e["payload"]["basis_hash"] == expected_bases[id]}
                for actor, review in latest.items():
                    for link, assessment in review["payload"].get("link_assessments", {}).items():
                        lines.extend([f"- Claim `{id}`, link `{link}`, reviewer `{_cell(actor)}`: "
                            f"`{assessment['judgment']}` / `{assessment['disposition']}`. "
                            f"{_cell(assessment['rationale'])} Evidence refs: {', '.join(assessment['evidence'])}."])
            lines.append("")
            if any(records for records, _ in findings.values()):
                lines.extend(["## Related review findings and recorded revisions", "",
                              "Acknowledging a related finding does not close its original reviewer veto.", ""])
                for id, (records, pending) in findings.items():
                    for review in records:
                        p = review["payload"]
                        status = "open related finding" if review["id"] in pending else "historical episode record"
                        lines.extend([f"- Context for `{id}`: review `{review['id']}` of `{p['claim']}` "
                            f"by `{_cell(review['actor'])}`, `{p['verdict']}`, **{status}**. "
                            f"{_cell(p['rationale'])} Recorded actions: {_cell('; '.join(p['actions']))}."])
                lines.append("")
        if literature is not None:
            lines.extend(_literature_manuscript(history, literature))
        lines.extend(["## Required author work before submission", "",
                      "Supply a verified literature review, explain the scientific contribution, check that "
                      "the registered methods match the implementation, and prepare the venue-specific "
                      "reproducibility and AI-use statements. Internal review is not external peer review.", ""])
        manuscript = self.store.put("\n".join(lines).encode("utf-8"))
        evidence_bundle = self.store.put_json(bundle)
        payload = dict(title=title, claims=claims, reviewed_bases=expected_bases, manuscript=manuscript,
                       bundle=evidence_bundle, source_snapshot=bundle["snapshot_hash"],
                       status="internal_draft")
        if literature is not None:
            payload.update(literature)
        return self.kernel._write(history, "paper", payload, {"writer"})

    def materialize(self, paper: str) -> dict[str, str]:
        history = self.store.events()
        event = Kernel._get(history, paper, "paper")
        payload = event["payload"]
        decisions = {id: self.kernel._next_action(history, id) for id in payload["reviewed_bases"]}
        status = paper_status(self.store, history, event, decisions)
        require(status == "current",
                f"paper review is no longer current or not eligible under current rules: {paper} ({status})")
        assert_current_support(history, payload)
        paths = {}
        # Materialize in root so relative evidence links remain valid.
        for field, extension in (("manuscript", "md"), ("bundle", "json")):
            path = self.store.root / f"{paper}.{extension}"
            _atomic_text(path, self.store.read(payload[field]).decode("utf-8"))
            paths[field] = str(path)
        return paths
