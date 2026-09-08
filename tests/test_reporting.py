"""Fail-closed publication scaffolding and coherent exports on generic metrics."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from episteme.kernel import Actor, GateError, Kernel
from episteme.reporting import PaperBuilder, export_store, inspect_store
from episteme.store import ConflictError, IntegrityError, Store


class ReportingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="episteme-report-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.planner = Kernel(self.store, Actor("planner", "planner"))
        self.reviewer = Kernel(self.store, Actor("reviewer", "reviewer"))
        self.scope = {"population": "test fixture"}
        hypotheses = [self.planner.hypothesis(x, x, x, self.scope) for x in ("positive", "null")]
        code = self.store.put(b"primary source fixture")
        self.environment = self.store.put_json({"python": "test fixture"})
        self.raw = self.store.put(b"value\n1\n3\n")
        self.code = code
        self.protocol = self.planner.preregister(
            hypotheses=hypotheses, scope=self.scope, design="Measure the saved mean",
            metric="mean", analysis_plan="Use all rows", stopping_rule="Fixed sample",
            seeds=[7], run_limit=6, implementation=code, environment=self.environment,
            data=self.raw, replication_tolerance=1e-8)
        self.executor = Kernel(self.store, Actor("executor", "executor"))
        primary = self.executor.start_run(self.protocol, seed=7, implementation=code,
            environment=self.environment, command=["fixture"])
        outputs = dict(raw_data=self.raw, metrics=self.store.put_json({"mean": 2.0}),
                       log=self.store.put(b"test fixture, not an actual experiment"))
        self.executor.finish_run(primary, status="completed", outputs=outputs)
        replicator = Kernel(self.store, Actor("replicator", "replicator"))
        replica = replicator.start_run(self.protocol, seed=7,
            implementation=self.store.put(b"independent source fixture"),
            environment=self.environment, command=["fixture"], replicate_of=primary)
        replicator.finish_run(replica, status="completed", outputs=outputs)
        self.claim = self.executor.claim(protocol=self.protocol, statement="Measured mean equals two",
            scope=self.scope, evidence=[primary, replica], limitations=["Test fixture only"], outcome="supports")
        self.bases = {self.claim: self.reviewer.gate(self.claim)["basis_hash"]}
        self.builder = PaperBuilder(self.store, Actor("writer", "writer"))

    def approve(self):
        return self.reviewer.review(self.claim, verdict="approve", rationale="Test-only review fixture",
                                   actions=[], expected_basis=self.bases[self.claim])

    def build(self):
        return self.builder.build(title="Test paper scaffold", claims=[self.claim], expected_bases=self.bases)

    def test_non_demo_metric_is_exported_with_value_and_evidence(self):
        history = self.store.events()
        export_store(self.store)
        report = (self.root / "report.md").read_text(encoding="utf-8")
        self.assertIn("| mean | 2.0 |", report)
        self.assertNotIn("Slope", report)
        self.assertIn(self.raw, report)
        self.assertEqual(self.store.events(), history)

    def test_export_uses_one_snapshot_even_when_writer_appends_during_export(self):
        before = self.store.events()
        original_read = self.store.read
        fired = False

        def read_then_append(key):
            nonlocal fired
            if not fired:
                fired = True
                self.planner.hypothesis("Later", "Later prediction", "Later falsifier", self.scope)
            return original_read(key)

        with patch.object(self.store, "read", side_effect=read_then_append):
            export_store(self.store)
        bundle = json.loads((self.root / "review-bundle.json").read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (self.root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(bundle["events"], before)
        self.assertEqual(events, before)
        self.assertEqual(bundle["snapshot_hash"], before[-1]["hash"])
        self.assertEqual(bundle["summary"]["event_count"], len(before))
        self.assertEqual(len(self.store.events()), len(before) + 1)

    def test_missing_review_cannot_build_paper(self):
        with self.assertRaisesRegex(GateError, "not eligible"):
            self.build()
        self.assertFalse(any(e["kind"] == "paper" for e in self.store.events()))

    def test_reviewed_paper_contains_original_claim_method_metric_and_limits(self):
        self.approve()
        paper = self.build()
        files = self.builder.materialize(paper)
        manuscript = Path(files["manuscript"]).read_text(encoding="utf-8")
        self.assertIn("Measured mean equals two", manuscript)
        self.assertIn("Measure the saved mean", manuscript)
        self.assertIn("| mean | 2.0 |", manuscript)
        self.assertIn("Test fixture only", manuscript)
        self.assertIn("not a submission-ready paper", manuscript)
        payload = Kernel._get(self.store.events(), paper, "paper")["payload"]
        self.assertEqual(self.store.read(payload["manuscript"]).decode(), manuscript)
        self.assertEqual(payload["reviewed_bases"], self.bases)

    def test_negative_review_prevents_build_and_materialization(self):
        self.approve()
        paper = self.build()
        self.reviewer.review(self.claim, verdict="request_changes", rationale="Found a confound",
                             actions=["Add a control"], expected_basis=self.bases[self.claim])
        with self.assertRaisesRegex(GateError, "not eligible"):
            self.build()
        with self.assertRaisesRegex(GateError, "no longer current"):
            self.builder.materialize(paper)

    def test_changed_evidence_and_tampering_prevent_materialization(self):
        self.approve()
        paper = self.build()
        (self.store.blobs / self.raw).write_bytes(b"corrupted")
        with self.assertRaisesRegex(GateError, "no longer current"):
            self.builder.materialize(paper)

    def test_stale_and_incomplete_requested_bases_are_rejected(self):
        self.approve()
        for bases in ({}, {self.claim: "stale"}):
            with self.subTest(bases=bases), self.assertRaises(GateError):
                self.builder.build(title="Test", claims=[self.claim], expected_bases=bases)

    def test_concurrent_mutation_prevents_paper_commit(self):
        self.approve()
        original_put = self.store.put
        fired = False

        def put_then_new_run(data):
            nonlocal fired
            if not fired:
                fired = True
                self.executor.start_run(self.protocol, seed=7, implementation=self.code,
                    environment=self.environment, command=["fixture"])
            return original_put(data)

        with patch.object(self.store, "put", side_effect=put_then_new_run):
            with self.assertRaises(ConflictError):
                self.build()
        self.assertFalse(any(e["kind"] == "paper" for e in self.store.events()))

    def test_readonly_store_supports_inspection_but_rejects_artifact_writes(self):
        history = self.store.events()
        with Store(self.root, read_only=True) as observer:
            self.assertEqual(inspect_store(observer)["event_count"], len(history))
            with self.assertRaisesRegex(IntegrityError, "read-only"):
                observer.put(b"forbidden")
        self.assertEqual(self.store.events(), history)

    def test_unknown_schema_version_is_not_silently_trusted(self):
        self.store.db.execute("DROP TRIGGER events_no_update")
        self.store.db.execute("UPDATE events SET schema_version = 999 WHERE seq = 1")
        self.store.db.commit()
        with self.assertRaises(IntegrityError):
            self.store.events()


if __name__ == "__main__":
    unittest.main()
