"""Recorded literature links. Fixture checks are not librarian or scientific review."""

import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from episteme.commands import CommandService
from episteme.graph import GraphIntegrityError, NodeKind, ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.reporting import PaperBuilder
from episteme.store import Store, digest
from review_paths import approve


STUDY = "fixture-literature"
SOURCE = "src/episteme/literature.py"


class LiteratureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="episteme-literature-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        planner = Kernel(self.store, Actor("planner", "planner"))
        scope = {"population": "literature fixture"}
        hypotheses = [planner.hypothesis(name, name, name, scope) for name in ("signal", "null")]
        code = self.store.put(b"primary source fixture")
        environment = self.store.put(b"environment fixture")
        raw = self.store.put(b"value\n1\n3\n")
        protocol = planner.preregister(
            hypotheses=hypotheses, scope=scope, design="Measure the saved mean", metric="mean",
            analysis_plan="Use all rows", stopping_rule="Fixed sample", seeds=[7], run_limit=6,
            implementation=code, environment=environment, data=raw, replication_tolerance=0.0)
        executor = Kernel(self.store, Actor("executor", "executor"))
        primary = executor.start_run(protocol, seed=7, implementation=code, environment=environment,
                                     command=["fixture"])
        outputs = dict(raw_data=raw, metrics=self.store.put_json({"mean": 2.0}),
                       log=self.store.put(b"test fixture, not an actual experiment"))
        executor.finish_run(primary, status="completed", outputs=outputs)
        replicator = Kernel(self.store, Actor("replicator", "replicator"))
        replica = replicator.start_run(
            protocol, seed=7, implementation=self.store.put(b"reanalysis source fixture"),
            environment=environment, command=["fixture"], replicate_of=primary)
        replicator.finish_run(replica, status="completed", outputs=outputs)
        self.claim = executor.claim(
            protocol=protocol, statement="Measured mean equals two", scope=scope,
            evidence=[primary, replica], limitations=["Test fixture only"], outcome="supports")
        self.bases = {self.claim: executor.gate(self.claim)["basis_hash"]}
        approve(self.store, self.claim, reviewer="reviewer", expected_basis=self.bases[self.claim])
        self.service = CommandService(self.store)

    def command(self, action, payload, *, actor, role, command_id):
        envelope = dict(
            context=dict(command_id=command_id, expected_revision=len(self.store.events()),
                         actor=actor, role=role, study_id=STUDY,
                         correlation_id="literature-fixture", causation_id=None),
            request=dict(version=1, action=action, payload=payload))
        return envelope, self.service.execute(envelope)

    def snapshot(self):
        return self.store.export(), self.store.export_receipts()

    def paper(self, citations=None, support=None, *, command_id="paper"):
        payload = dict(title="Fixture scaffold", claims=[self.claim], expected_bases=self.bases)
        if citations is not None:
            payload["citations"] = citations
        if support is not None:
            payload["support"] = support
        return self.command("paper.build", payload, actor="writer", role="writer", command_id=command_id)

    def source_and_locator(self, *, suffix="1", locator="fixture:passage-1"):
        _, source = self.command(
            "literature.record_source",
            dict(title="Fixture source", year=2020, authors=["Fixture Author"]),
            actor="fixture-recorder", role="planner", command_id=f"source-{suffix}")
        _, located = self.command(
            "literature.record_locator",
            dict(source=source, locator_kind="fixture", locator=locator),
            actor="fixture-recorder", role="planner", command_id=f"locator-{suffix}")
        return source, located

    def test_literature_module_does_not_import_a_network_library(self):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath(SOURCE).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
        for module in modules:
            self.assertNotIn(module.split(".")[0], {"socket", "urllib", "http", "requests", "httpx"}, module)

    def test_a_string_is_not_a_manuscript_citation(self):
        before = self.snapshot()
        for citations in ("Smith 1999", ["Smith 1999"]):
            with self.subTest(citations=citations):
                with self.assertRaises((ValueError, GateError)):
                    self.paper(citations, command_id="paper-string")
                self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(GateError, "not a bare string"):
            self.paper(["Smith 1999"], command_id="paper-string")

    def test_unverified_locator_cannot_support_a_claim(self):
        _, locator = self.source_and_locator()
        _, citation = self.command("literature.cite", dict(locator=locator),
                                   actor="writer", role="writer", command_id="cite-unverified")
        before = self.snapshot()
        with self.assertRaisesRegex(GateError, "unverified locator cannot support"):
            self.paper([citation], [citation], command_id="paper-unverified")
        self.assertEqual(self.snapshot(), before)
        _, paper = self.paper([citation], command_id="paper-listed")
        manuscript = self.store.read(Kernel._get(self.store.events(), paper, "paper")["payload"]["manuscript"]).decode()
        self.assertIn("Locator verification status: `unverified`.", manuscript)
        self.assertIn("Not used as support for a scientific claim.", manuscript)
        self.assertIn("An unverified locator cannot support a claim.", manuscript)
        self.assertIn("Missing literature is not novelty and is not support for a scientific claim.", manuscript)
        self.assertNotIn("is novel", manuscript)

    def test_contradicted_locator_cannot_support_a_claim(self):
        _, locator = self.source_and_locator()
        passage = self.store.put(b"fixture passage bytes")
        sha = digest(b"fixture:passage-1")
        self.command("literature.record_check", dict(
            locator=locator, locator_sha256=sha, passage_digest=passage,
            outcome="verified_by_recorded_check", checker_kind="fixture",
            statement="Fixture statement naming the passage."),
            actor="fixture-recorder", role="planner", command_id="check-verified")
        self.command("literature.record_check", dict(
            locator=locator, locator_sha256=sha, passage_digest=passage,
            outcome="contradicted", checker_kind="fixture",
            statement="Fixture statement that the passage does not match."),
            actor="fixture-recorder", role="planner", command_id="check-contradicted")
        _, citation = self.command("literature.cite", dict(locator=locator),
                                   actor="writer", role="writer", command_id="cite-contradicted")
        before = self.snapshot()
        with self.assertRaisesRegex(GateError, "contradicted locator cannot support"):
            self.paper([citation], [citation], command_id="paper-contradicted")
        self.assertEqual(self.snapshot(), before)

    def test_check_must_name_the_exact_locator_bytes(self):
        _, locator = self.source_and_locator()
        passage = self.store.put(b"fixture passage bytes")
        before = self.snapshot()
        with self.assertRaisesRegex(GateError, "does not name the locator bytes"):
            self.command("literature.record_check", dict(
                locator=locator, locator_sha256=digest(b"other locator"), passage_digest=passage,
                outcome="verified_by_recorded_check", checker_kind="fixture",
                statement="Fixture statement."),
                actor="fixture-recorder", role="planner", command_id="check-wrong-bytes")
        self.assertEqual(self.snapshot(), before)

    def test_recorded_fixture_check_is_shown_and_replay_is_idempotent(self):
        def offline(*_args, **_kwargs):
            raise AssertionError("network")

        with patch("socket.create_connection", offline), patch("urllib.request.urlopen", offline):
            source_envelope, source = self.command(
                "literature.record_source",
                dict(title="Fixture source", year=2020, authors=["Fixture Author"]),
                actor="fixture-recorder", role="planner", command_id="source-ok")
            _, locator = self.command(
                "literature.record_locator",
                dict(source=source, locator_kind="fixture", locator="fixture:passage-1"),
                actor="fixture-recorder", role="planner", command_id="locator-ok")
            _, claim = self.command(
                "literature.record_claim",
                dict(locator=locator, statement="The fixture passage contains one bounded sentence."),
                actor="fixture-recorder", role="planner", command_id="claim-ok")
            passage = self.store.put(b"fixture passage bytes")
            sha = digest("fixture:passage-1".encode("utf-8"))
            check_envelope, check = self.command("literature.record_check", dict(
                locator=locator, locator_sha256=sha, passage_digest=passage,
                outcome="verified_by_recorded_check", checker_kind="fixture",
                statement="Fixture statement naming these passage bytes."),
                actor="fixture-recorder", role="planner", command_id="check-ok")
            cite_envelope, citation = self.command(
                "literature.cite", dict(locator=locator),
                actor="writer", role="writer", command_id="cite-ok")
            paper_envelope, paper = self.paper([citation], [citation], command_id="paper-ok")
            count = len(self.store.events())
            self.assertEqual(self.service.execute(source_envelope), source)
            self.assertEqual(self.service.execute(check_envelope), check)
            self.assertEqual(self.service.execute(cite_envelope), citation)
            self.assertEqual(self.service.execute(paper_envelope), paper)
            self.assertEqual(len(self.store.events()), count)

        history = self.store.events()
        for kind in ("literature_source", "literature_locator", "literature_claim",
                     "literature_citation", "literature_check"):
            event = next(item for item in history if item["kind"] == kind)
            self.assertEqual(event["payload"]["scientific_validity"], "not_assessed")
            self.assertNotIn("novelty", event["payload"])
            self.assertNotIn("novel", event["payload"])
        self.assertNotIn("novelty", Kernel._get(history, paper, "paper")["payload"])
        self.assertEqual(history[[item["id"] for item in history].index(claim)]["payload"]["extraction_actor"],
                         "fixture-recorder")
        payload = Kernel._get(history, paper, "paper")["payload"]
        manuscript = self.store.read(payload["manuscript"]).decode()
        self.assertIn(f"Citation `{citation}`", manuscript)
        self.assertIn("fixture:passage-1", manuscript)
        self.assertIn("Fixture source", manuscript)
        self.assertIn("The fixture passage contains one bounded sentence.", manuscript)
        self.assertIn("not a scientific finding and not a novelty assessment", manuscript)
        self.assertIn("verified_by_recorded_check", manuscript)
        self.assertIn("A fixture check is not a librarian's verification and not a scientific review.", manuscript)
        self.assertIn("Missing literature is not novelty and is not support for a scientific claim.", manuscript)
        self.assertNotIn("is novel", manuscript)
        self.assertEqual(payload["support"], [citation])
        self.assertEqual(payload["support_checks"][citation], check)
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(citation).kind, NodeKind.LITERATURE_CITATION)
        self.assertEqual(graph.node(check).kind, NodeKind.LITERATURE_CHECK)
        PaperBuilder(self.store, Actor("writer", "writer")).materialize(paper)

        self.command("literature.record_check", dict(
            locator=locator, locator_sha256=sha, passage_digest=passage,
            outcome="contradicted", checker_kind="fixture",
            statement="Later fixture statement that the passage is contradicted."),
            actor="fixture-recorder", role="planner", command_id="check-later")
        with self.assertRaisesRegex(GateError, "contradicted locator cannot support"):
            PaperBuilder(self.store, Actor("writer", "writer")).materialize(paper)
        papers = [event["id"] for event in self.store.events() if event["kind"] == "paper"]
        with self.assertRaisesRegex(GateError, "contradicted locator cannot support"):
            self.paper([citation], [citation], command_id="paper-after-contradiction")
        self.assertEqual([event["id"] for event in self.store.events() if event["kind"] == "paper"], papers)
        self.assertEqual(self.service.execute(paper_envelope), paper)
        self.assertEqual(graph.node(source).kind, NodeKind.LITERATURE_SOURCE)
        ResearchGraph.from_store(self.store)

    def test_paper_without_citations_keeps_the_historical_payload(self):
        envelope, paper = self.paper(command_id="paper-plain")
        receipt = next(row for row in self.store.receipts() if row["command_id"] == "paper-plain")
        self.assertEqual(set(receipt["request"]["payload"]), {"title", "claims", "expected_bases"})
        payload = Kernel._get(self.store.events(), paper, "paper")["payload"]
        self.assertEqual(set(payload), {"title", "claims", "reviewed_bases", "manuscript", "bundle",
                                        "source_snapshot", "status"})
        manuscript = self.store.read(payload["manuscript"]).decode()
        self.assertNotIn("Recorded literature", manuscript)
        count = len(self.store.events())
        self.assertEqual(self.service.execute(envelope), paper)
        self.assertEqual(len(self.store.events()), count)

    def test_literature_event_without_a_receipt_fails_the_graph(self):
        self.store.append(id="literature_source-forged0001", kind="literature_source",
                          actor="planner", role="planner", payload={"forged": True},
                          expected_revision=len(self.store.events()))
        with self.assertRaisesRegex(GraphIntegrityError, "literature record lacks its original command receipt"):
            ResearchGraph.from_store(self.store)


if __name__ == "__main__":
    unittest.main()
