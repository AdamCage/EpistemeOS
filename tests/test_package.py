"""Read-only reproduction package. Every opinion and metric here is a fixture."""

import ast
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from episteme.cli import main
from episteme.commands import CommandService
from episteme.execution import freeze_environment
from episteme.kernel import Actor, Kernel
from episteme.package import UNKNOWN_REASON, render_results, verify_package, write_package
from episteme.store import Store, digest
from review_paths import approve


STUDY = "fixture-package"
SOURCE = Path(__file__).resolve().parents[1] / "src" / "episteme" / "package.py"


def _tree(path: Path) -> dict[str, bytes]:
    return {item.relative_to(path).as_posix(): item.read_bytes()
            for item in path.rglob("*") if item.is_file()}


def _command(store: Store, actor: str, role: str, action: str, payload: dict) -> str:
    return CommandService(store).execute(dict(
        context=dict(command_id=f"{action}-{uuid4().hex}", expected_revision=len(store.events()),
                     actor=actor, role=role, study_id=STUDY, correlation_id="fixture-package",
                     causation_id=None),
        request=dict(version=1, action=action, payload=payload)))


def _sections(text: str) -> dict[str, str]:
    sections = {}
    for part in text.split("\n## ")[1:]:
        title, _, body = part.partition("\n")
        sections[title.strip()] = body
    return sections


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-package-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "store")
        self.addCleanup(self.store.close)
        self.planner = Kernel(self.store, Actor("fixture-planner", "planner"))
        self.executor = Kernel(self.store, Actor("fixture-executor", "executor"))
        self.replicator = Kernel(self.store, Actor("fixture-replicator", "replicator"))
        self.scope = {"dataset": "synthetic", "population": "two fixture units"}
        self.hypotheses = [self.planner.hypothesis(name, name, name, self.scope) for name in ("zero", "nonzero")]
        self.code = self.store.put(b"fixture primary implementation")
        self.environment = self.store.put(b"fixture environment")
        self.data = self.store.put(b"unit,difference\nu1,-1\nu2,1\n")

    def protocol(self, *, environment: str | None = None):
        return self.planner.preregister(
            hypotheses=self.hypotheses, scope=self.scope, design="Synthetic difference experiment",
            metric="mean_difference", analysis_plan="Arithmetic mean of the two unit differences",
            stopping_rule="Registered fixture stopping", seeds=[7], run_limit=8,
            implementation=self.code, environment=environment or self.environment, data=self.data,
            replication_tolerance=0.0, statistical_design=None)

    def runs(self, protocol: str):
        failed = self.executor.start_run(protocol, seed=7, implementation=self.code,
                                         environment=self.event_environment(protocol), command=["fixture"])
        failed_metrics = self.store.put_json({"mean_difference": -5.0})
        self.executor.finish_run(failed, status="failed", reason="numerical instability", outputs={
            "log": self.store.put(b"numerical instability"),
            "raw_data": self.store.put(b"raw -5.0"), "metrics": failed_metrics})
        completed = self.executor.start_run(protocol, seed=7, implementation=self.code,
                                            environment=self.event_environment(protocol), command=["fixture"])
        completed_metrics = self.store.put_json({"mean_difference": 0})
        self.executor.finish_run(completed, status="completed", outputs={
            "raw_data": self.data, "metrics": completed_metrics,
            "log": self.store.put(b"fixture result")})
        replica = self.replicator.start_run(
            protocol, seed=7, implementation=self.store.put(b"fixture reanalysis implementation"),
            environment=self.event_environment(protocol), command=["fixture"], replicate_of=completed)
        self.replicator.finish_run(replica, status="completed", outputs={
            "raw_data": self.data, "metrics": completed_metrics,
            "log": self.store.put(b"fixture reanalysis")})
        claim = self.executor.claim(
            protocol=protocol, statement="Fixture mean is zero; population behavior unknown",
            scope=self.scope, evidence=[completed, replica],
            limitations=["Synthetic fixture, declared actor separation only"], outcome="inconclusive")
        return dict(failed=failed, completed=completed, failed_metrics=failed_metrics,
                    completed_metrics=completed_metrics, claim=claim)

    def event_environment(self, protocol: str) -> str:
        return next(event["payload"]["environment"] for event in self.store.events()
                    if event["id"] == protocol)

    def cite(self, *, suffix: str, locator: str, outcome: str | None):
        source = _command(self.store, "fixture-recorder", "planner", "literature.record_source",
                          dict(title="Fixture source", year=2020, authors=["Fixture Author"]))
        located = _command(self.store, "fixture-recorder", "planner", "literature.record_locator",
                           dict(source=source, locator_kind="fixture", locator=locator))
        if outcome is not None:
            passage = self.store.put(b"fixture passage bytes")
            _command(self.store, "fixture-recorder", "planner", "literature.record_check", dict(
                locator=located, locator_sha256=digest(locator.encode("utf-8")),
                passage_digest=passage, outcome=outcome, checker_kind="fixture",
                statement=f"Fixture statement {suffix}."))
        return _command(self.store, "writer", "writer", "literature.cite", dict(locator=located))

    def current(self):
        protocol = self.protocol()
        built = self.runs(protocol)
        approve(self.store, built["claim"], reviewer="fixture-reviewer")
        verified = self.cite(suffix="verified", locator="fixture:verified", outcome="verified_by_recorded_check")
        unverified = self.cite(suffix="unverified", locator="fixture:unverified", outcome=None)
        contradicted = self.cite(suffix="contradicted", locator="fixture:contradicted", outcome="contradicted")
        basis = self.executor.gate(built["claim"])["basis_hash"]
        paper = _command(self.store, "writer", "writer", "paper.build", dict(
            title="Fixture draft", claims=[built["claim"]], expected_bases={built["claim"]: basis},
            citations=[verified, unverified, contradicted], support=[verified]))
        built.update(protocol=protocol, paper=paper, verified=verified, unverified=unverified,
                     contradicted=contradicted)
        return built

    def export(self, name: str) -> Path:
        path = self.root / name
        write_package(self.store, self.claim, path)
        return path

    def test_package_lists_failures_fixtures_and_keeps_citations_apart(self):
        built = self.current()
        self.claim = built["claim"]
        before = self.store.export(), self.store.export_receipts()
        first = self.export("first")
        second = self.export("second")
        self.assertEqual(_tree(first), _tree(second))
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)
        verify_package(first)
        readme = (first / "README.md").read_text(encoding="utf-8")
        self.assertIn("This directory is not a venue submission.", readme)
        self.assertNotIn("generated_at", "".join(item.read_text(encoding="utf-8")
                                                  for item in first.rglob("*.json")))
        attempts = json_of(first / "attempts.json")
        by_run = {row["run"]: row for row in attempts["attempts"]}
        self.assertEqual(by_run[built["failed"]]["status"], "failed")
        self.assertEqual(by_run[built["failed"]]["recorded_reason"], "numerical instability")
        self.assertEqual(by_run[built["completed"]]["status"], "completed")
        self.assertTrue(attempts["rerun_seeds"])
        inventory = (first / "attempts.md").read_text(encoding="utf-8")
        self.assertIn(built["failed"], inventory)
        self.assertIn("failed", inventory)
        self.assertIn("numerical instability", inventory)
        self.assertIn(built["completed"], inventory)
        results = (first / "results.md").read_text(encoding="utf-8")
        self.assertIn(built["failed_metrics"], results)
        self.assertIn("-5.0", results)
        self.assertIn(built["completed_metrics"], results)
        papers = json_of(first / "papers.json")
        self.assertEqual(papers["papers"], [dict(
            id=built["paper"], hash=next(event["hash"] for event in self.store.events()
                                          if event["id"] == built["paper"]),
            status="current", title="Fixture draft", claims=[built["claim"]],
            manuscript_sha256=next(event["payload"]["manuscript"] for event in self.store.events()
                                   if event["id"] == built["paper"]),
            text_included=True, text_path=f"papers/{built['paper']}.md",
            scientific_validity=None, scientific_validity_stored=False)])
        manuscript = self.store.read(papers["papers"][0]["manuscript_sha256"])
        self.assertEqual((first / "papers" / f"{built['paper']}.md").read_bytes(), manuscript)
        reviews = json_of(first / "reviews.json")["records"]
        self.assertTrue(any(row["label"] == "fixture" and row["counted"] for row in reviews))
        self.assertIn("no scientific review", reviews[0]["rationale"].casefold())
        literature = json_of(first / "literature.json")
        self.assertEqual(literature["support"], [built["verified"]])
        self.assertEqual(literature["unverified"], [built["unverified"]])
        self.assertEqual(literature["contradicted"], [built["contradicted"]])
        self.assertNotIn(built["unverified"], literature["support"])
        self.assertNotIn(built["contradicted"], literature["support"])
        sections = _sections((first / "literature.md").read_text(encoding="utf-8"))
        self.assertIn(built["verified"], sections["Support"])
        self.assertNotIn(built["unverified"], sections["Support"])
        self.assertIn(built["unverified"], sections["Unverified locators"])
        self.assertIn(built["contradicted"], sections["Contradicted locators"])
        self.assertNotIn(built["contradicted"], sections["Support"])
        self.assertIn("not a librarian's verification", sections["Support"])
        self.assertFalse(json_of(first / "pack.json")["bound"])
        self.assertFalse(json_of(first / "execution.json")["profile_v2_used"])
        self.assertEqual(json_of(first / "execution.json")["closures"], [])
        self.assertTrue(all(value in (None, "not_assessed") for value in _validity(json_of(first / "family.json"))
                            + _validity(json_of(first / "reviews.json"))
                            + _validity(json_of(first / "literature.json"))
                            + _validity(papers)))
        changed = (first / "README.md").read_bytes().replace(b"venue", b"venuX", 1)
        (first / "README.md").write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "README.md"):
            verify_package(first)

    def test_a_later_attempt_makes_the_old_paper_historical(self):
        built = self.current()
        self.claim = built["claim"]
        protocol = built["protocol"]
        later = self.executor.start_run(protocol, seed=7, implementation=self.code,
                                        environment=self.event_environment(protocol), command=["fixture"])
        self.executor.finish_run(later, status="completed", outputs={
            "raw_data": self.data, "metrics": self.store.put_json({"mean_difference": 0}),
            "log": self.store.put(b"later fixture")})
        before = self.store.export(), self.store.export_receipts()
        path = self.export("historical")
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)
        papers = json_of(path / "papers.json")["papers"]
        self.assertEqual([row["status"] for row in papers], ["historical"])
        self.assertFalse(papers[0]["text_included"])
        self.assertFalse((path / "papers").exists())
        manuscript = self.store.read(papers[0]["manuscript_sha256"])
        self.assertNotIn(manuscript, _tree(path).values())

    def test_an_ineligible_historical_paper_is_not_the_current_paper(self):
        import json
        from golden_support import load_history
        fixture = json.loads((Path(__file__).resolve().parent / "fixtures" / "adr0018"
                              / "legacy_review_paper.json").read_text(encoding="utf-8"))
        self.store.close()
        store = load_history(fixture, self.root / "legacy")
        self.addCleanup(store.close)
        claim = next(event["id"] for event in store.events() if event["kind"] == "claim")
        paper = next(event for event in store.events() if event["kind"] == "paper")
        before = store.export(), store.export_receipts()
        path = self.root / "legacy-package"
        write_package(store, claim, path)
        self.assertEqual((store.export(), store.export_receipts()), before)
        rows = json_of(path / "papers.json")["papers"]
        self.assertEqual(rows[0]["id"], paper["id"])
        self.assertEqual(rows[0]["status"], "not_eligible_under_current_rules")
        self.assertFalse(rows[0]["text_included"])
        self.assertNotIn(store.read(paper["payload"]["manuscript"]), _tree(path).values())
        self.assertIn("This directory is not a venue submission.",
                      (path / "README.md").read_text(encoding="utf-8"))

    def test_unknown_attempt_is_listed_with_its_recorded_reason(self):
        protocol = self.protocol(environment=freeze_environment(self.store))
        built = self.runs(protocol)
        job = _command(self.store, "fixture-executor", "executor", "execution.enqueue", dict(
            protocol=protocol, seed=7, outputs={"raw_data": "raw.bin", "metrics": "metrics.json"},
            wall_seconds=30, max_output_bytes=65536))
        _command(self.store, "fixture-executor", "executor", "execution.dispatch",
                 dict(job=job, workspace_token=uuid4().hex))
        unknown = next(event["payload"]["run"] for event in self.store.events() if event["id"] == job)
        before = self.store.export()
        path = self.root / "unknown-package"
        write_package(self.store, built["claim"], path)
        self.assertEqual(self.store.export(), before)
        attempts = json_of(path / "attempts.json")["attempts"]
        row = next(item for item in attempts if item["run"] == unknown)
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["recorded_reason"], UNKNOWN_REASON)
        self.assertIn(UNKNOWN_REASON, (path / "attempts.md").read_text(encoding="utf-8"))
        self.assertIn(built["failed"], (path / "attempts.md").read_text(encoding="utf-8"))
        execution = json_of(path / "execution.json")
        self.assertEqual(execution["jobs"][0]["profile"], "trusted_local_python_v1")
        self.assertIsNone(execution["jobs"][0]["environment_closure"])
        self.assertFalse(execution["profile_v2_used"])
        self.assertEqual(execution["closures"], [])
        self.assertFalse(json_of(path / "pack.json")["bound"])

    def test_a_metric_without_a_source_digest_is_omitted(self):
        text, gaps = render_results([dict(
            tier="family", run="run-1", primary_metric="mean_difference",
            metric_value=1.25, metrics=None, status="completed")])
        self.assertNotIn("1.25", text)
        self.assertEqual(gaps, ["Run run-1 metric mean_difference has no source digest; the number is omitted."])
        self.assertNotIn("1.25", gaps[0])

    def test_cli_export_appends_no_events(self):
        built = self.current()
        count = len(self.store.events())
        snapshot = self.store.export()
        root = self.store.root
        self.store.close()
        out = self.root / "cli-package"
        with redirect_stdout(io.StringIO()):
            status = main(["package", built["claim"], "--output", str(out), "--root", str(root)])
        self.assertEqual(status, 0)
        verify_package(out)
        with Store(root, read_only=True) as reopened:
            self.assertEqual(len(reopened.events()), count)
            self.assertEqual(reopened.export(), snapshot)

    def test_package_output_inside_the_store_is_refused(self):
        built = self.current()
        before = self.store.export()
        with self.assertRaisesRegex(ValueError, "outside the research store"):
            write_package(self.store, built["claim"], self.store.root / "inside")
        self.assertEqual(self.store.export(), before)

    def test_package_module_does_not_import_a_network_library(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
        for module in modules:
            self.assertNotIn(module.split(".")[0], {"socket", "urllib", "http", "requests", "httpx"}, module)


def json_of(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def _validity(value):
    found = []
    if isinstance(value, dict):
        if "scientific_validity" in value:
            found.append(value["scientific_validity"])
        for item in value.values():
            found.extend(_validity(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_validity(item))
    return found


if __name__ == "__main__":
    unittest.main()
