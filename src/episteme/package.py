"""Read-only reproduction package for one claim family (ADR 0022).

``episteme package`` writes a directory of JSON and Markdown. It appends no
events and does not submit anything to a venue. The directory is not a
scientific approval, a replication, or a statement that actors, context, or
the operating system were isolated. Role labels are not that isolation.

A paper scaffold is copied only when ``paper_status`` is ``current``.
``scientific_validity`` is copied from stored fields and is never upgraded.
Failed and unknown attempts stay in the inventory. A results table prints a
number only together with the digest of the artifact it was read from.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .kernel import Actor, Kernel, require
from .literature import locator_status
from .reporting import admission_record, paper_status
from .review_admission import admission, attempt_ledger
from .store import Store, digest


SCHEMA = "reproduction-package-v1"
# The same sentence execution.job_state records for a dispatch with no completion.
UNKNOWN_REASON = "dispatch exists; no verified terminal evidence; never auto-relaunch"
_SHA = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,80}")
_PROFILE_V2 = "uv_locked_python_v2"


def _sha(value: Any) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _text(value: str) -> bytes:
    if not value.endswith("\n"):
        value += "\n"
    return value.encode("utf-8")


def _json(value: Any) -> bytes:
    return _text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _stored(payload: dict[str, Any]) -> tuple[Any, bool]:
    """The scientific_validity field as stored. Absence stays absence."""
    if "scientific_validity" in payload:
        return payload["scientific_validity"], True
    return None, False


def _fixture_rationale(text: Any) -> bool:
    """The recorded-rationale test used by the cycle controller. A label, not a grade."""
    if type(text) is not str:
        return False
    folded = text.casefold()
    return "fixture" in folded and "no scientific review" in folded


def _metric_gap(row: dict[str, Any]) -> str | None:
    value = row.get("metric_value")
    if type(value) not in (int, float):
        return None
    if _sha(row.get("metrics")):
        return None
    return (f"Run {row.get('run')} metric {row.get('primary_metric')} has no source digest; "
            "the number is omitted.")


def render_results(attempts: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Markdown results table. A number without a metrics digest is omitted."""
    lines = ["# Results", "",
             "Each number in this table was read from the metrics artifact named in that row. "
             "A number with no source digest is omitted and listed as a gap. "
             "This table is not a figure and not a scientific conclusion.", ""]
    emitted = []
    gaps = []
    for row in attempts:
        if row.get("tier") != "family":
            continue
        gap = _metric_gap(row)
        if gap:
            gaps.append(gap)
            continue
        value = row.get("metric_value")
        if type(value) not in (int, float):
            continue
        emitted.append((row["run"], row.get("primary_metric"), json.dumps(value), row["metrics"]))
    if emitted:
        lines.extend(["| Run | Metric | Value | Source digest |",
                      "| --- | --- | --- | --- |"])
        lines.extend(f"| `{run}` | `{_cell(metric)}` | {value} | `{source}` |"
                     for run, metric, value, source in emitted)
    else:
        lines.append("No metric value with a source digest is recorded.")
    lines.extend(["", "## Gaps", ""])
    lines.extend(f"- {gap}" for gap in gaps)
    if not gaps:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines), gaps


def _reason(history: list[dict[str, Any]], row: dict[str, Any]) -> str | None:
    if row.get("reason"):
        return row["reason"]
    if row.get("status") != "unknown":
        return None
    jobs = {event["id"] for event in history
            if event["kind"] == "execution_job" and event["payload"].get("run") == row.get("run")}
    if any(event["kind"] == "execution_dispatch" and event["payload"].get("job") in jobs
           for event in history):
        return UNKNOWN_REASON
    return None


def _annotate(history: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated = []
    for row in rows:
        copy = dict(row)
        copy["recorded_reason"] = _reason(history, row)
        annotated.append(copy)
    return annotated


def _without_unsourced_numbers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop a metric value that has no metrics digest so the number is not stored either."""
    exported = []
    for row in rows:
        copy = dict(row)
        if _metric_gap(row):
            copy["metric_value"] = None
        exported.append(copy)
    return exported


def _reruns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(protocol=row["protocol"], seed=row["seed"], run=row["run"],
                 primary_attempt=row["primary_attempt"])
            for row in rows
            if row.get("tier") == "family" and row.get("primary_attempt", 1) > 1]


def _attempt_markdown(rows: list[dict[str, Any]], reruns: list[dict[str, Any]],
                      disclosures: list[str]) -> str:
    lines = ["# Attempt inventory", "",
             "Every recorded attempt on this claim family's protocols, including completed, "
             "failed, unknown, and rerun seeds. Related registrations are listed separately "
             "and are not family evidence. Same-code replay, a fresh-seed repetition, "
             "an independent reanalysis, and a new-data replication are not relabeled as each other.",
             ""]
    lines.extend(["| Run | Status | Recorded reason | Seed | Kind | Origin |",
                  "| --- | --- | --- | --- | --- | --- |"])
    family = [row for row in rows if row.get("tier") == "family"]
    for row in family:
        kind = (f"primary attempt {row['primary_attempt']}" if row.get("kind") == "primary"
                else f"reanalysis of {row.get('replicate_of')}")
        reason = row.get("recorded_reason") or "no recorded reason"
        lines.append(f"| `{row['run']}` | {_cell(row.get('status'))} | {_cell(reason)} | "
                     f"{row.get('seed')} | {_cell(kind)} | {_cell(row.get('outputs_origin'))} |")
    if not family:
        lines.append("| none |  |  |  |  |  |")
    lines.extend(["", "## Rerun seeds", ""])
    if reruns:
        lines.extend(f"- seed {row['seed']} of `{row['protocol']}` primary attempt "
                     f"{row['primary_attempt']} (`{row['run']}`)" for row in reruns)
    else:
        lines.append("- none")
    lines.extend(["", "## Related registrations", "",
                  "Protocols that share a hypothesis and have no protocol edge. "
                  "They are disclosed and are not evidence of this family.", ""])
    related = [row for row in rows if row.get("tier") != "family"]
    if related:
        lines.extend(f"- `{row['run']}` protocol `{row['protocol']}` seed {row.get('seed')} "
                     f"status {_cell(row.get('status'))}; recorded reason: "
                     f"{_cell(row.get('recorded_reason') or 'no recorded reason')}."
                     for row in related)
    else:
        lines.append("- none")
    lines.extend(["", "## Disclosures", ""])
    lines.extend(f"- {_cell(item)}" for item in disclosures)
    if not disclosures:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _object(store: Store, key: str, label: str) -> Any:
    require(_sha(key), f"invalid {label} digest")
    try:
        return json.loads(store.read(key))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} is not stored JSON") from exc


def _checks(history: list[dict[str, Any]], locator: dict[str, Any]) -> list[dict[str, Any]]:
    sha = digest(locator["payload"]["locator"].encode("utf-8"))
    return [event for event in history
            if event["kind"] == "literature_check"
            and event["payload"].get("locator") == locator["id"]
            and event["payload"].get("locator_hash") == locator["hash"]
            and event["payload"].get("locator_sha256") == sha]


def _citations(history: list[dict[str, Any]], papers: list[dict[str, Any]]) -> dict[str, Any]:
    order: list[str] = []
    current_support: list[str] = []
    for paper in papers:
        payload = paper["payload"]
        for citation_id in payload.get("citations") or []:
            if citation_id not in order:
                order.append(citation_id)
        if paper["status"] == "current":
            for citation_id in payload.get("support") or []:
                if citation_id not in current_support:
                    current_support.append(citation_id)
    records = []
    support, unverified, contradicted, other = [], [], [], []
    for citation_id in order:
        citation = Kernel._get(history, citation_id, "literature_citation")
        locator = Kernel._get(history, citation["payload"]["locator"], "literature_locator")
        source = Kernel._get(history, locator["payload"]["source"], "literature_source")
        status = locator_status(history, locator["id"])
        checks = _checks(history, locator)
        validity, stored = _stored(citation["payload"])
        fixture = any(event["payload"].get("checker_kind") == "fixture" for event in checks)
        record = dict(id=citation["id"], hash=citation["hash"],
                      locator=locator["id"], locator_hash=locator["hash"],
                      locator_kind=locator["payload"].get("locator_kind"),
                      locator_text=locator["payload"].get("locator"),
                      source=source["id"], title=source["payload"].get("title"),
                      year=source["payload"].get("year"),
                      authors=list(source["payload"].get("authors") or []),
                      status=status, fixture_check=fixture,
                      scientific_validity=validity, scientific_validity_stored=stored)
        records.append(record)
        if status == "unverified":
            unverified.append(citation_id)
        elif status == "contradicted":
            contradicted.append(citation_id)
        elif status == "verified_by_recorded_check" and citation_id in current_support:
            support.append(citation_id)
        else:
            other.append(citation_id)
    return dict(citations=records, support=support, unverified=unverified,
                contradicted=contradicted, other=other)


def _literature_markdown(section: dict[str, Any]) -> str:
    by_id = {row["id"]: row for row in section["citations"]}

    def bullets(ids: list[str], empty: str) -> list[str]:
        if not ids:
            return [empty, ""]
        lines = []
        for citation_id in ids:
            row = by_id[citation_id]
            lines.append(f"- Citation `{citation_id}` locator `{row['locator']}` "
                         f"({_cell(row['locator_kind'])} `{_cell(row['locator_text'])}`) "
                         f"source `{row['source']}` \"{_cell(row['title'])}\" ({row['year']}). "
                         f"Status `{row['status']}`. "
                         f"scientific_validity stored: `{row['scientific_validity_stored']}`, "
                         f"value `{row['scientific_validity']}`.")
            if row["fixture_check"]:
                lines.append("  A fixture check is not a librarian's verification and not a scientific review.")
        lines.append("")
        return lines

    lines = ["# Recorded citations", "",
             "Citations in this package are stored citation records. "
             "A locator string written only in prose is not a citation. "
             "Missing literature is not novelty and is not support for a scientific claim.",
             "", "## Support", "",
             "Only a citation listed as support by a paper that is current now, whose locator "
             "status is `verified_by_recorded_check`, appears here.", ""]
    lines.extend(bullets(section["support"], "- none"))
    lines.extend(["## Unverified locators", "",
                  "These locators are not in the support section.", ""])
    lines.extend(bullets(section["unverified"], "- none"))
    lines.extend(["## Contradicted locators", "",
                  "These locators are not in the support section. "
                  "A recorded contradiction is not removed by another check.", ""])
    lines.extend(bullets(section["contradicted"], "- none"))
    lines.extend(["## Other recorded citations", "",
                  "Stored citations that are not current support. "
                  "A historical paper's support list is not moved here into support.", ""])
    lines.extend(bullets(section["other"], "- none"))
    return "\n".join(lines)


def _papers(store: Store, history: list[dict[str, Any]], family: set[str]) -> list[dict[str, Any]]:
    reader = Kernel(store, Actor("package-reader", "observer"))
    rows = []
    for event in history:
        if event["kind"] != "paper":
            continue
        payload = event["payload"]
        claims = list(payload.get("claims") or [])
        if not family.intersection(claims):
            continue
        bases = payload.get("reviewed_bases") or {}
        decisions = {claim_id: reader._next_action(history, claim_id) for claim_id in bases}
        status = paper_status(store, history, event, decisions)
        validity, stored = _stored(payload)
        safe = _SAFE_ID.fullmatch(event["id"]) is not None
        include = status == "current" and safe and _sha(payload.get("manuscript"))
        rows.append(dict(event=event, status=status, include=include, validity=validity,
                         stored=stored, path=f"papers/{event['id']}.md" if include else None))
    return rows


def _jobs(store: Store, history: list[dict[str, Any]], runs: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from .environment_closure import is_closure
    closures: dict[str, Any] = {}
    rows = []
    for event in history:
        if event["kind"] != "execution_job" or event["payload"].get("run") not in runs:
            continue
        payload = event["payload"]
        spec = _object(store, payload["specification"], "execution specification")
        run = Kernel._get(history, payload["run"], "run")
        environment = _object(store, run["payload"]["environment"], "run environment")
        profile = spec.get("profile") if type(spec.get("profile")) is str else None
        if profile is None and type(environment) is dict:
            if type(environment.get("profile")) is str:
                profile = environment["profile"]
            elif type(environment.get("backend")) is str:
                profile = environment["backend"]
        version = spec.get("schema_version")
        closure = None
        if version == 2 and _sha(spec.get("environment")):
            closure = spec["environment"]
            declaration = _object(store, closure, "environment closure")
            if is_closure(declaration):
                closures[closure] = declaration
        elif type(environment) is dict and is_closure(environment):
            closure = run["payload"]["environment"]
            closures[closure] = environment
        result = next((item for item in history
                       if item["kind"] == "result" and item["payload"].get("run") == run["id"]), None)
        record = None
        if result is not None and _sha(result["payload"].get("outputs", {}).get("environment_record")):
            record = result["payload"]["outputs"]["environment_record"]
        validity, stored = _stored(payload)
        rows.append(dict(job=event["id"], job_hash=event["hash"], run=run["id"],
                         profile=profile, schema_version=version,
                         specification=payload["specification"],
                         environment_closure=closure, environment_record=record,
                         interpreter_fingerprint=spec.get("environment_fingerprint") if version == 1 else None,
                         scientific_validity=validity, scientific_validity_stored=stored))
    return rows, closures


def _packs(store: Store, history: list[dict[str, Any]], protocols: list[str],
           closures: dict[str, Any]) -> list[dict[str, Any]]:
    from .domain_packs import pack_lineage
    from .environment_closure import is_closure
    found: dict[str, dict[str, Any]] = {}
    for protocol in protocols:
        binding = pack_lineage(history, protocol)
        if binding is not None:
            found[binding["id"]] = binding
    rows = []
    for binding in found.values():
        payload = binding["payload"]
        code_key = payload.get("pack_code_digest")
        manifest = _object(store, code_key, "pack code manifest") if _sha(code_key) else None
        environment = payload.get("environment")
        if _sha(environment):
            declaration = _object(store, environment, "pack environment")
            if type(declaration) is dict and is_closure(declaration):
                closures[environment] = declaration
        validity, stored = _stored(payload)
        rows.append(dict(binding=binding["id"], binding_hash=binding["hash"],
                         protocol=payload.get("protocol"), pack_id=payload.get("pack_id"),
                         pack_version=payload.get("pack_version"), pack_code_digest=code_key,
                         execution_profile=payload.get("execution_profile"),
                         closure_level=payload.get("closure_level"), environment=environment,
                         code_manifest=manifest, scientific_validity=validity,
                         scientific_validity_stored=stored))
    return rows


def _reviews(store: Store, history: list[dict[str, Any]], claims: list[str]) -> list[dict[str, Any]]:
    by_id = {event["id"]: event for event in history}
    rows = []
    for claim in claims:
        record = admission_record(store, history, claim)
        for approval in record["approvals"]:
            event = by_id.get(approval["review"])
            payload = {} if event is None else event["payload"]
            validity, stored = _stored(payload)
            rows.append(dict(claim=claim, review=approval["review"],
                             reviewer=approval["reviewer"], counted=approval["counted"],
                             label="fixture" if _fixture_rationale(approval.get("rationale")) else "recorded_approval",
                             rationale=approval.get("rationale"), defect=approval.get("defect"),
                             scientific_validity=validity, scientific_validity_stored=stored))
        for veto in record["vetoes"]:
            rows.append(dict(claim=claim, review=veto["opinion"], reviewer=veto["reviewer"],
                             counted=False, label="veto", rationale=None, defect=None,
                             scientific_validity=None, scientific_validity_stored=False))
    return rows


def _readme(claim: str, snapshot: str) -> str:
    return "\n".join([
        "# Reproduction package", "",
        "This directory is not a venue submission.",
        "It is a read-only export of one claim family. It is not a manuscript release, "
        "not peer review, and not a scientific result.", "",
        f"Claim `{claim}`. Snapshot `{snapshot}`.", "",
        "The export appends no events. Building it does not use a network, a model, or a GPU.", "",
        "`scientific_validity` is copied from stored event fields and is never upgraded. "
        "A missing field stays missing. Mechanical completeness, a passing gate, tournament priority, "
        "and a successful run are not scientific validity.", "",
        "Failed, unknown, and completed attempts are listed, with the recorded reason when one was stored. "
        "A rerun of a seed is listed as a rerun. Omitting a failed or unknown attempt is a defect of this export. "
        "Same-code replay, a fresh-seed repetition, an independent reanalysis, and a new-data replication "
        "are different records. This package does not relabel one as another.", "",
        "A paper scaffold is included only when current rules say that paper is `current`. "
        "A historical or ineligible paper is named with that status. Its old text is not the current paper.", "",
        "An approval whose recorded rationale says it is a fixture is labeled `fixture`. "
        "That label is not a scientific grade. Different role names do not prove that actors, "
        "review context, or the operating system were isolated. This export adds no such isolation.", "",
        "Citations are stored citation records. Unverified and contradicted locators are listed "
        "apart from support and are not moved into support. Missing literature is not novelty.", "",
        "Figures, LaTeX, venue templates, and automatic bibliography formatting are not in this package. "
        "A results table names the source artifact digest of every number it prints. "
        "A number with no source digest is omitted.", "",
        "The manifest records the SHA-256 of every other file in this directory. "
        "`manifest.json` is the hash list and is not a member of that list.", ""])


def _analysis_reports(history: list[dict[str, Any]], family: set[str]) -> list[dict[str, Any]]:
    rows = []
    for event in history:
        payload = event["payload"]
        if payload.get("claim") not in family:
            continue
        if event["kind"] == "pack_analysis":
            for label in ("report", "statistical_report"):
                if _sha(payload.get(label)):
                    rows.append(dict(id=event["id"], kind=event["kind"], claim=payload["claim"],
                                     label=label, sha256=payload[label]))
        elif event["kind"] == "batch_analysis" and _sha(payload.get("proposal_digest")):
            rows.append(dict(id=event["id"], kind=event["kind"], claim=payload["claim"],
                             label="proposal", sha256=payload["proposal_digest"]))
    return rows


def _body(store: Store, claim: str) -> dict[str, bytes]:
    history = store.events()
    Kernel._get(history, claim, "claim")
    projection = admission(store, history, replay=False)
    family = list(projection.family(claim))
    require(claim in family, "claim is not in its family")
    ledger = attempt_ledger(store, history, claim)
    attempts = _annotate(history, ledger["attempts"])
    reruns = _reruns(attempts)
    snapshot = history[-1]["hash"] if history else "0" * 64
    papers = _papers(store, history, set(family))
    literature = _citations(history, [dict(row["event"], status=row["status"]) for row in papers])
    protocol_ids = [row["id"] for row in ledger["family_protocols"]]
    protocols = []
    for row in ledger["family_protocols"]:
        event = Kernel._get(history, row["id"], "protocol")
        require(row["hash"] == event["hash"], "protocol hash differs from the stored event")
        payload = event["payload"]
        protocols.append(dict(id=row["id"], hash=event["hash"], edge=row["edge"],
                              implementation=payload.get("implementation"),
                              environment=payload.get("environment"), data=payload.get("data")))
    run_ids = {row["run"] for row in attempts if row.get("tier") == "family"}
    jobs, closures = _jobs(store, history, run_ids)
    packs = _packs(store, history, protocol_ids, closures)
    v2 = (any(row.get("profile") == _PROFILE_V2 or row.get("schema_version") == 2 for row in jobs)
          or any(row.get("execution_profile") == _PROFILE_V2 for row in packs) or bool(closures))
    results, gaps = render_results(attempts)
    exported_attempts = _without_unsourced_numbers(attempts)
    claims = []
    for claim_id in family:
        event = Kernel._get(history, claim_id, "claim")
        validity, stored = _stored(event["payload"])
        claims.append(dict(id=claim_id, hash=event["hash"], outcome=event["payload"].get("outcome"),
                           scientific_validity=validity, scientific_validity_stored=stored))
    files: dict[str, bytes] = {}
    files["README.md"] = _text(_readme(claim, snapshot))
    files["family.json"] = _json(dict(
        schema=SCHEMA, claim=claim, family=family, claims=claims, snapshot_hash=snapshot,
        event_count=len(history), family_ledger_digest=ledger["family_ledger_digest"],
        not_a_venue_submission=True))
    files["attempts.json"] = _json(dict(
        schema=SCHEMA, claim=claim, attempts=exported_attempts, rerun_seeds=reruns,
        disclosures=list(ledger["disclosures"]),
        family_ledger_digest=ledger["family_ledger_digest"]))
    files["attempts.md"] = _text(_attempt_markdown(attempts, reruns, list(ledger["disclosures"])))
    files["protocols.json"] = _json(dict(
        schema=SCHEMA, protocols=protocols,
        related_registrations=list(ledger["related_registrations"])))
    files["execution.json"] = _json(dict(
        schema=SCHEMA, profile_v2_used=v2, jobs=jobs,
        closures=[dict(sha256=key, declaration=closures[key]) for key in sorted(closures)],
        note=("Profile v2 environment closures are the stored closure declarations. "
              "Profile v1 stores an interpreter fingerprint, not an environment closure. "
              "No closure is invented when profile v2 was not used.")))
    files["pack.json"] = _json(dict(
        schema=SCHEMA, bound=bool(packs), bindings=packs,
        note=("Each binding includes the stored pack code manifest. "
              "No manifest is included when no pack is bound.")))
    raw = [dict(run=row["run"], sha256=row["raw_data"]) for row in attempts
           if row.get("tier") == "family" and _sha(row.get("raw_data"))]
    metrics = [dict(run=row["run"], sha256=row["metrics"]) for row in attempts
               if row.get("tier") == "family" and _sha(row.get("metrics"))]
    files["artifacts.json"] = _json(dict(
        schema=SCHEMA, raw_outputs=raw, metrics=metrics,
        analysis_reports=_analysis_reports(history, set(family)), result_gaps=gaps))
    files["results.md"] = _text(results)
    files["reviews.json"] = _json(dict(schema=SCHEMA, records=_reviews(store, history, family)))
    files["literature.json"] = _json(dict(schema=SCHEMA, **literature))
    files["literature.md"] = _text(_literature_markdown(literature))
    paper_rows = []
    for row in papers:
        payload = row["event"]["payload"]
        paper_rows.append(dict(id=row["event"]["id"], hash=row["event"]["hash"], status=row["status"],
                               title=payload.get("title"), claims=list(payload.get("claims") or []),
                               manuscript_sha256=payload.get("manuscript"),
                               text_included=row["include"], text_path=row["path"],
                               scientific_validity=row["validity"],
                               scientific_validity_stored=row["stored"]))
        if row["include"]:
            files[row["path"]] = store.read(payload["manuscript"])
    files["papers.json"] = _json(dict(
        schema=SCHEMA, papers=paper_rows,
        note=("The scaffold text is written only for a paper whose status is current. "
              "Historical and ineligible papers are listed here and their old text is not "
              "the current paper.")))
    return files


def package_files(store: Store, claim: str) -> dict[str, bytes]:
    """Every file of the package, including the manifest. No events are written."""
    files = _body(store, claim)
    rows = [dict(path=path, sha256=digest(data), bytes=len(data)) for path, data in sorted(files.items())]
    files["manifest.json"] = _json(dict(
        schema=SCHEMA, files=rows,
        note="SHA-256 of every other file in the package. This manifest is not a member of the list."))
    return files


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_package(store: Store, claim: str, destination: Path) -> dict[str, Any]:
    """Write the package outside the store. The store is not modified."""
    root = store.root.resolve()
    dest = Path(destination)
    if dest.exists() or dest.is_symlink():
        raise ValueError("package output already exists")
    resolved = dest.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError("package output must be outside the research store")
    files = package_files(store, claim)
    dest.mkdir(parents=True)
    for name in sorted(files):
        _atomic(dest / name, files[name])
    family = json.loads(files["family.json"])
    return dict(claim=claim, output=str(dest), files=sorted(files),
                snapshot_hash=family["snapshot_hash"], events_appended=0, venue_submission=False)


def verify_package(directory: Path) -> None:
    """Recompute every listed file hash. A mismatch or an extra file fails."""
    root = Path(directory)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("package manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    listed = manifest.get("files")
    if type(listed) is not list:
        raise ValueError("package manifest has no file list")
    expected = {}
    for row in listed:
        if type(row) is not dict or not _sha(row.get("sha256")) or type(row.get("path")) is not str:
            raise ValueError("package manifest row is invalid")
        expected[row["path"]] = row
    present = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if present != set(expected) | {"manifest.json"}:
        raise ValueError("package files differ from the manifest")
    for path, row in expected.items():
        data = (root / path).read_bytes()
        if digest(data) != row["sha256"] or len(data) != row["bytes"]:
            raise ValueError(f"package file does not match the manifest: {path}")
