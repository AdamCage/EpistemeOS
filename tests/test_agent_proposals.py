"""Proposal bytes and scientific-role boundaries; no provider/model calls."""

from copy import deepcopy
import json
import unittest

from episteme.agent_proposals import (
    MAX_CANDIDATES, MAX_COMPARISON_LENGTH, MAX_LIMITATION_LENGTH, MAX_LIMITATIONS,
    MAX_TEXT_LENGTH, PROMPT_VERSION, PROPOSAL_SCHEMA, STRUCTURED_OUTPUT_SCHEMA,
    SYSTEM_PROMPT, ProposalValidationError, validate_proposal,
)


class AgentProposalTests(unittest.TestCase):
    def proposed(self):
        return dict(status="proposed", reason="Compare a mechanism with a null alternative.",
            candidates=[dict(statement="Изменение отсутствует.", prediction="No systematic change.",
                             falsifier="Stable change outside the control range.", kind="null"),
                        dict(statement="A specified mechanism changes the measurement.",
                             prediction="The control removes the change.",
                             falsifier="The change survives removal of the mechanism.", kind="mechanism")],
            comparison_plan="Compare the registered control and treatment within the frozen scope.",
            limitations=["Hypotheses only; no experiments or scientific review have been performed."])

    def parse(self, value):
        return validate_proposal(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def test_proposal_preserves_unicode_and_accepted_text_verbatim(self):
        proposal = self.proposed()
        proposal["reason"] = "  Bounded proposal, with preserved caller text.  "
        result = self.parse(proposal)
        self.assertEqual(result, proposal)
        self.assertIsNot(result, proposal)
        self.assertEqual(result["candidates"][0]["kind"], "null")

    def test_relabeling_identical_content_does_not_create_a_competing_explanation(self):
        proposal = self.proposed()
        proposal["candidates"][1] = dict(proposal["candidates"][0], kind="mechanism")
        with self.assertRaisesRegex(ProposalValidationError, "duplicate candidate"):
            self.parse(proposal)

    def test_abstention_has_reason_and_limitations_without_placeholder_candidates(self):
        proposal = dict(status="abstained", reason="The question lacks an observable outcome.", candidates=[],
                        comparison_plan="", limitations=["No measurable comparison can be specified."])
        self.assertEqual(self.parse(proposal), proposal)
        for field, value in (("candidates", self.proposed()["candidates"]), ("comparison_plan", "invented plan"),
                             ("reason", ""), ("limitations", [])):
            with self.subTest(field=field), self.assertRaises(ProposalValidationError):
                self.parse(dict(proposal, **{field: value}))

    def test_strict_json_rejects_duplicate_keys_at_root_and_inside_candidate(self):
        encoded = json.dumps(self.proposed())
        duplicates = [encoded.replace('"status": "proposed"', '"status": "abstained", "status": "proposed"'),
                      encoded.replace('"kind": "null"', '"kind": "mechanism", "kind": "null"')]
        for raw in duplicates:
            with self.subTest(raw=raw), self.assertRaisesRegex(ProposalValidationError, "duplicate JSON key"):
                validate_proposal(raw.encode())

    def test_strict_json_rejects_nonfinite_numbers_including_overflow_literals(self):
        for number in ("NaN", "Infinity", "-Infinity", "1e999"):
            raw = json.dumps(self.proposed()).replace('"status": "proposed"', f'"status": {number}')
            with self.subTest(number=number), self.assertRaisesRegex(ProposalValidationError, "nonfinite"):
                validate_proposal(raw.encode())

    def test_transport_formatting_and_wrong_root_types_are_not_silently_repaired(self):
        encoded = json.dumps(self.proposed()).encode()
        for raw in (b"\xff", b"\xef\xbb\xbf" + encoded, b"```json\n" + encoded + b"\n```",
                    encoded + b" trailing explanation", b"[]", b"null", b"true", b"{}"):
            with self.subTest(raw=raw[:30]), self.assertRaises(ProposalValidationError):
                validate_proposal(raw)
        with self.assertRaises(ProposalValidationError):
            validate_proposal(encoded.decode())

    def test_exact_fields_are_required_at_both_levels(self):
        for field in self.proposed():
            candidate = self.proposed()
            del candidate[field]
            with self.subTest(missing=field), self.assertRaises(ProposalValidationError):
                self.parse(candidate)
        with self.assertRaises(ProposalValidationError):
            self.parse(dict(self.proposed(), approve=True))
        for field in self.proposed()["candidates"][0]:
            proposal = self.proposed()
            del proposal["candidates"][0][field]
            with self.subTest(candidate_missing=field), self.assertRaises(ProposalValidationError):
                self.parse(proposal)
        proposal = self.proposed()
        proposal["candidates"][0]["scope"] = {"population": "model cannot change the question scope"}
        with self.assertRaises(ProposalValidationError):
            self.parse(proposal)

    def test_candidate_count_and_distinct_null_nonnull_contract(self):
        for count in (0, 1, MAX_CANDIDATES + 1):
            proposal = self.proposed()
            proposal["candidates"] = [dict(proposal["candidates"][index % 2], statement=f"Alternative {index}")
                                      for index in range(count)]
            with self.subTest(count=count), self.assertRaises(ProposalValidationError):
                self.parse(proposal)
        for kind in ("null", "mechanism", "confound", "artifact"):
            proposal = self.proposed()
            for candidate in proposal["candidates"]:
                candidate["kind"] = kind
            with self.subTest(kind=kind), self.assertRaises(ProposalValidationError):
                self.parse(proposal)
        proposal = self.proposed()
        proposal["candidates"] = [dict(proposal["candidates"][index % 2], statement=f"Alternative {index}")
                                  for index in range(MAX_CANDIDATES)]
        self.assertEqual(len(self.parse(proposal)["candidates"]), MAX_CANDIDATES)

    def test_duplicate_candidates_and_unknown_kinds_are_rejected(self):
        proposal = self.proposed()
        proposal["candidates"].append(deepcopy(proposal["candidates"][0]))
        with self.assertRaisesRegex(ProposalValidationError, "duplicate candidate"):
            self.parse(proposal)
        for value in ("NULL", "causal", None, 1):
            proposal = self.proposed()
            proposal["candidates"][0]["kind"] = value
            with self.subTest(kind=value), self.assertRaises(ProposalValidationError):
                self.parse(proposal)

    def test_text_types_empty_text_and_limits_are_validated_without_coercion(self):
        for value in (None, False, 7, [], {}, "", " \t\n", "\u2003", "x" * (MAX_TEXT_LENGTH + 1)):
            for field in ("reason", "candidate.prediction"):
                proposal = self.proposed()
                if field.startswith("candidate."):
                    proposal["candidates"][0]["prediction"] = value
                else:
                    proposal[field] = value
                with self.subTest(value=repr(value)[:30], field=field), self.assertRaises(ProposalValidationError):
                    self.parse(proposal)
        proposal = self.proposed()
        proposal["reason"] = "x" * MAX_TEXT_LENGTH
        proposal["comparison_plan"] = "x" * MAX_COMPARISON_LENGTH
        self.assertEqual(self.parse(proposal), proposal)
        proposal["comparison_plan"] += "x"
        with self.assertRaises(ProposalValidationError):
            self.parse(proposal)

    def test_limitations_remain_explicit_bounded_nonempty_strings(self):
        for limitations in (None, "unknown", [], [None], [" "], ["x" * (MAX_LIMITATION_LENGTH + 1)],
                            ["bounded uncertainty"] * (MAX_LIMITATIONS + 1)):
            with self.subTest(limitations=repr(limitations)[:40]), self.assertRaises(ProposalValidationError):
                self.parse(dict(self.proposed(), limitations=limitations))

    def test_provider_schema_is_structurally_strict_but_semantic_schema_is_explicitly_stronger(self):
        schema = STRUCTURED_OUTPUT_SCHEMA
        self.assertEqual(schema["type"], "object")
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), set(self.proposed()))
        candidate = schema["properties"]["candidates"]["items"]
        self.assertIs(candidate["additionalProperties"], False)
        self.assertEqual(set(candidate["required"]), set(self.proposed()["candidates"][0]))
        self.assertNotIn("allOf", schema)
        self.assertIn("allOf", PROPOSAL_SCHEMA)
        # Shape-compatible empty comparison plans still cannot reach application.
        with self.assertRaises(ProposalValidationError):
            self.parse(dict(self.proposed(), comparison_plan=""))
        changed = deepcopy(schema)
        changed["properties"]["reason"]["type"] = "null"
        self.assertEqual(PROPOSAL_SCHEMA["properties"]["reason"]["type"], "string")

    def test_prompt_declares_proposals_and_abstention_instead_of_scientific_approval(self):
        self.assertEqual(PROMPT_VERSION, "hypothesis-proposal-v1")
        for phrase in ("frozen research question", '"abstained"', "not established findings", "Do not invent"):
            self.assertIn(phrase, SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
