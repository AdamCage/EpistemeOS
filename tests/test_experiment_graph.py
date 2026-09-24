"""Focused projection/admission checks for frozen experiment-agent provenance."""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from episteme.commands import _check_study
from episteme.experiment_proposals import PROPOSAL_SCHEMA
from episteme.graph import GraphIntegrityError, Relation, _Projection
from episteme.store import canonical


def _event(id, kind, seq, payload):
    return dict(id=id, kind=kind, seq=seq, hash=f"{seq:064x}", payload=payload)


def _history():
    events = [
        _event("question", "research_question", 1, dict(study_id="study-a")),
        _event("set", "explanation_set", 2, dict(study_id="study-a", question="question")),
        _event("tree", "search_tree", 3, {}),
        _event("budget", "agent_budget", 4, dict(study_id="study-a")),
        _event("request", "agent_request", 5, dict(
            schema_version=2, budget="budget", budget_hash=f"{4:064x}",
            question="question", question_hash=f"{1:064x}",
            explanation_set="set", explanation_set_hash=f"{2:064x}",
            tree="tree", tree_hash=f"{3:064x}", recipe_binding="a" * 64)),
        _event("dispatch", "agent_dispatch", 6, dict(schema_version=1, request="request")),
        _event("response", "agent_response", 7, dict(schema_version=1, request="request")),
        _event("protocol", "protocol", 8, dict(
            planning=dict(study_id="study-a", explanation_set="set"))),
        _event("node", "experiment_node", 9, dict(tree="tree", protocol="protocol")),
        _event("application", "agent_application", 10, dict(
            schema_version=2, request="request", request_hash=f"{5:064x}",
            response="response", response_hash=f"{7:064x}",
            protocol="protocol", protocol_hash=f"{8:064x}",
            experiment_node="node", experiment_node_hash=f"{9:064x}",
            compilation="b" * 64, limitations=["Synthetic graph fixture"],
            scientific_validity="not_assessed")),
    ]
    return events


class ExperimentGraphTests(unittest.TestCase):
    def test_published_proposal_schema_matches_validator_contract(self):
        path = Path(__file__).resolve().parents[1] / "schemas" / "experiment-proposal-v1.schema.json"
        published = path.read_bytes()
        self.assertEqual(published, canonical(PROPOSAL_SCHEMA) + b"\n")
        self.assertEqual(json.loads(published), PROPOSAL_SCHEMA)

    def project(self, history, index):
        projection = _Projection(None, history)
        projection.event = history[index]
        with patch("episteme.agents.agent_artifacts", return_value=set()):
            projection.project()
        return projection

    def test_request_and_application_project_typed_frozen_provenance(self):
        history = _history()
        request = self.project(history, 4)
        self.assertEqual({(edge.source, edge.target, edge.field) for edge in request.edges}, {
            ("budget", "request", "budget"), ("question", "request", "question"),
            ("set", "request", "explanation_set"), ("tree", "request", "tree")})
        application = self.project(history, 9)
        typed = {(edge.source, edge.target, edge.field) for edge in application.edges
                 if edge.relation == Relation.AGENT_REFERENCE}
        generated = {(edge.source, edge.target) for edge in application.edges
                     if edge.relation == Relation.AGENT_GENERATED}
        self.assertEqual(typed, {("request", "application", "request"),
                                 ("response", "application", "response"),
                                 ("protocol", "application", "protocol"),
                                 ("node", "application", "experiment_node")})
        self.assertEqual(generated, {("response", "protocol"), ("response", "node")})
        self.assertTrue(all(edge.scientific_validity == "not_assessed" for edge in application.edges))

    def test_application_rejects_changed_hash_and_cross_tree_node(self):
        history = _history()
        changed = copy.deepcopy(history)
        changed[-1]["payload"]["protocol_hash"] = "f" * 64
        with self.assertRaisesRegex(GraphIntegrityError, "reference hash mismatch"):
            self.project(changed, 9)
        changed = copy.deepcopy(history)
        changed[-2]["payload"]["tree"] = "different-tree"
        with self.assertRaisesRegex(GraphIntegrityError, "differs from its frozen request"):
            self.project(changed, 9)

    def test_request_rejects_changed_set_hash_or_wrong_tree_kind(self):
        history = _history()
        changed = copy.deepcopy(history)
        changed[4]["payload"]["explanation_set_hash"] = "f" * 64
        with self.assertRaisesRegex(GraphIntegrityError, "reference hash mismatch"):
            self.project(changed, 4)
        changed = copy.deepcopy(history)
        changed[2]["kind"] = "experiment_node"
        with self.assertRaisesRegex(GraphIntegrityError, "wrong reference kind"):
            self.project(changed, 4)

    def test_study_admission_follows_v2_set_tree_and_application_refs(self):
        history = _history()
        _check_study(history, "agent.dispatch", {"request": "request"}, "study-a")
        changed = copy.deepcopy(history)
        changed[1]["payload"]["study_id"] = "study-b"
        with self.assertRaisesRegex(ValueError, "study_id differs"):
            _check_study(changed, "agent.dispatch", {"request": "request"}, "study-a")
        changed = copy.deepcopy(history)
        changed[7]["payload"]["planning"]["study_id"] = "study-b"
        with self.assertRaisesRegex(ValueError, "study_id differs"):
            _check_study(changed, "agent.dispatch", {"request": "request"}, "study-a")
        with self.assertRaisesRegex(ValueError, "study_id differs"):
            _check_study(changed, "agent.apply_experiment", {"request": "application"}, "study-a")


if __name__ == "__main__":
    unittest.main()
