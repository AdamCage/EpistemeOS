"""Snapshot-consistent evidence exports and internally reviewed paper scaffolds."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .kernel import Actor, Kernel, require
from .store import Store, canonical


FIXTURE_NOTICE = (
    "Synthetic fixture; no LLM/API calls or independent AI agents. "
    "Different fixture actor IDs and separately executed implementations exercise "
    "the kernel contract. Reanalysis uses the same observations, not new data. "
    "Mechanical checks do not assess scientific validity."
)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


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
    return result


def inspect_store(store: Store) -> dict[str, Any]:
    return _summary(store, store.events())


def artifact_inventory(store: Store, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys: set[str] = set()
    for event in history:
        p = event["payload"]
        if event["kind"] == "protocol":
            keys.update(p[key] for key in ("implementation", "environment", "data"))
        elif event["kind"] == "run":
            keys.update(p[key] for key in ("implementation", "environment"))
        elif event["kind"] == "result":
            keys.update(p["outputs"].values())
        elif event["kind"] == "paper":
            keys.update((p["manuscript"], p["bundle"]))
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
    summary = _summary(store, history)
    return dict(bundle_version=1, summary=summary, events=history,
                artifacts=artifact_inventory(store, history),
                scientific_review="not performed by export", snapshot_hash=summary["last_event_hash"])


def _run_table(store: Store, history: list[dict[str, Any]], runs: list[dict[str, Any]]) -> list[str]:
    rows = ["| Run | Kind | Seed | Primary metric | Value | Raw data | Implementation |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for run in runs:
        p = run["payload"]
        plan = Kernel._get(history, p["protocol"], "protocol")["payload"]
        result = Kernel._result(history, run["id"])
        status = result["payload"]["status"] if result else "running"
        if status != "completed":
            rows.append(f"| {run['id']} | {status} | {p['seed']} | {_cell(plan['metric'])} | — | — | — |")
            continue
        outputs = result["payload"]["outputs"]
        metrics = json.loads(store.read(outputs["metrics"]))
        kind = "reanalysis of same data" if p["replicate_of"] else "primary"
        rows.append(f"| {run['id']} | {kind} | {p['seed']} | {_cell(plan['metric'])} | "
                    f"{_cell(metrics[plan['metric']])} | "
                    f"[raw](artifacts/sha256/{outputs['raw_data']}) | "
                    f"[source](artifacts/sha256/{p['implementation']}) |")
    return rows


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
    report.extend(["## Recorded computations", "", *_run_table(
        store, history, [e for e in history if e["kind"] == "run"]), "",
        "Frozen protocols, runtime records and artifact hashes: [review-bundle.json](review-bundle.json).", ""])
    files = {"review-bundle.json": json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
             "report.md": "\n".join(report),
             "events.jsonl": "".join(canonical(event).decode() + "\n" for event in history)}
    for name, content in files.items():
        _atomic_text(store.root / name, content)
    return {name: str(store.root / name) for name in files}


class PaperBuilder:
    """Build a traceable internal draft, never an automatic venue submission."""

    def __init__(self, store: Store, actor: Actor):
        self.kernel = Kernel(store, actor)
        self.store = store

    def build(self, *, title: str, claims: list[str], expected_bases: dict[str, str]) -> str:
        history = self.store.events()
        require(isinstance(title, str) and title.strip(), "paper title required")
        require(bool(claims) and len(set(claims)) == len(claims), "paper requires unique claims")
        require(set(expected_bases) == set(claims), "paper requires a reviewed basis for every claim")
        decisions = {id: self.kernel._next_action(history, id) for id in claims}
        for id, decision in decisions.items():
            require(decision["action"] == "paper_candidate", f"claim not eligible for paper: {id}")
            require(decision["basis_hash"] == expected_bases[id], f"stale paper evidence: {id}")
        bundle = review_bundle(self.store, history)
        bundle.update(selected_claims=claims, reviewed_bases=expected_bases,
                      paper_status="internal evidence-linked scaffold; human release pending")
        lines = [f"# {_cell(title)}", "", "**Internal evidence-linked draft.** "
                 "This scaffold records reviewed claims and computations. It is not a submission-ready paper.", "",
                 "## Research record", "", f"Source snapshot: `{bundle['snapshot_hash']}`.", ""]
        for id in claims:
            c = Kernel._get(history, id, "claim")["payload"]
            p = Kernel._get(history, c["protocol"], "protocol")["payload"]
            lines.extend([f"## Result {id}", "", c["statement"], "",
                          f"Recorded outcome: `{c['outcome']}`. Scope: `{json.dumps(c['scope'], ensure_ascii=False)}`.",
                          "", f"Protocol: `{c['protocol']}`. Review basis: `{expected_bases[id]}`.", "",
                          "### Registered methods", "", p["design"], "", p["analysis_plan"], "",
                          f"Stopping rule: {p['stopping_rule']}", "", "### Evidence", ""])
            runs = [e for e in history if e["kind"] == "run" and e["payload"]["protocol"] == c["protocol"]]
            lines.extend(_run_table(self.store, history, runs))
            lines.extend(["", "### Limitations", "", *(f"- {item}" for item in c["limitations"]), ""])
        lines.extend(["## Required author work before submission", "",
                      "Supply a verified literature review, explain the scientific contribution, check that "
                      "the registered methods match the implementation, and prepare the venue-specific "
                      "reproducibility and AI-use statements. Internal review is not external peer review.", ""])
        manuscript = self.store.put("\n".join(lines).encode("utf-8"))
        evidence_bundle = self.store.put_json(bundle)
        return self.kernel._write(history, "paper", dict(title=title, claims=claims,
            reviewed_bases=expected_bases, manuscript=manuscript, bundle=evidence_bundle,
            source_snapshot=bundle["snapshot_hash"], status="internal_draft"), {"writer"})

    def materialize(self, paper: str) -> dict[str, str]:
        history = self.store.events()
        payload = Kernel._get(history, paper, "paper")["payload"]
        for id, basis in payload["reviewed_bases"].items():
            decision = self.kernel._next_action(history, id)
            require(decision["action"] == "paper_candidate" and decision["basis_hash"] == basis,
                    f"paper review is no longer current: {id}")
        paths = {}
        # Materialize in root so relative evidence links remain valid.
        for field, extension in (("manuscript", "md"), ("bundle", "json")):
            path = self.store.root / f"{paper}.{extension}"
            _atomic_text(path, self.store.read(payload[field]).decode("utf-8"))
            paths[field] = str(path)
        return paths
