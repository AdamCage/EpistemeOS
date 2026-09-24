"""Frozen experiment proposals and provider output boundaries; no model calls."""

from copy import deepcopy
import json
import unittest

from episteme.experiment_proposals import (
    MAX_LIMITATIONS, MAX_LIMITATION_LENGTH, MAX_SAMPLES, MAX_TEXT_LENGTH,
    MIN_SAMPLES, PROMPT_VERSION, PROPOSAL_SCHEMA, RECIPE_ID,
    STRUCTURED_OUTPUT_SCHEMA, SYSTEM_PROMPT, ProposalValidationError,
    validate_proposal,
)
from episteme.search import COMPONENTS


IDS = ["hypothesis-null", "hypothesis-mechanism", "hypothesis-confound"]


class ExperimentProposalTests(unittest.TestCase):
    def proposed(self):
        return {
            "schema_version": 1,
            "status": "proposed",
            "reason": "This exploratory contrast may separate the mechanisms.",
            "experiment": {
                "recipe_id": RECIPE_ID,
                "parameters": {"n_samples": 128, "assignment": "randomized", "analysis": "difference_in_means"},
                "mode": "exploratory",
                "action": "discriminate",
                "hypothesis_predictions": [
                    {"hypothesis": IDS[0], "expected_observation": "Effect near zero."},
                    {"hypothesis": IDS[1], "expected_observation": "Positive treatment effect."},
                    {"hypothesis": IDS[2], "expected_observation": "Effect changes with the confound."},
                ],
                "discriminating_contrast": "Compare randomized treatment with a controlled baseline.",
                "rationale": "A difference separates the null from the specified mechanism.",
                "components": {key: 0.5 for key in COMPONENTS},
            },
            "limitations": ["Synthetic data cannot establish an external causal effect."],
        }

    def parse(self, value, ids=IDS):
        return validate_proposal(json.dumps(value, ensure_ascii=False).encode("utf-8"), ids)

    def test_valid_proposal_preserves_text_and_all_frozen_hypotheses(self):
        proposal = self.proposed()
        proposal["experiment"]["hypothesis_predictions"][0]["expected_observation"] = "  Нулевой эффект.  "
        self.assertEqual(self.parse(proposal), proposal)
        self.assertIsNot(self.parse(proposal), proposal)
        self.assertEqual(len(self.parse(proposal)["experiment"]["hypothesis_predictions"]), len(IDS))

    def test_abstention_preserves_reason_and_has_no_placeholder_experiment(self):
        proposal = {"schema_version": 1, "status": "abstained", "reason": "No feasible synthetic contrast.",
                    "experiment": None, "limitations": ["The known hypotheses lack measurable predictions."]}
        self.assertEqual(self.parse(proposal), proposal)
        for field, value in (("experiment", self.proposed()["experiment"]),
                             ("reason", " \t"), ("limitations", [])):
            with self.subTest(field=field), self.assertRaises(ProposalValidationError):
                self.parse(dict(proposal, **{field: value}))
        with self.assertRaises(ProposalValidationError):
            self.parse(dict(self.proposed(), experiment=None))

    def test_frozen_ids_are_ordered_unique_and_required_even_for_abstention(self):
        proposal = self.proposed()
        for ids in ([], [IDS[0]], [IDS[0], IDS[0]], "not a list", [IDS[0], " "]):
            with self.subTest(ids=ids), self.assertRaises(ProposalValidationError):
                self.parse(proposal, ids)
        with self.assertRaises(ProposalValidationError):
            self.parse(proposal, tuple(reversed(IDS)))

    def test_predictions_must_match_every_frozen_id_once_in_order(self):
        for mutation in ("missing", "extra", "duplicate", "wrong_id", "reordered"):
            proposal = self.proposed()
            predictions = proposal["experiment"]["hypothesis_predictions"]
            if mutation == "missing":
                predictions.pop()
            elif mutation == "extra":
                predictions.append({"hypothesis": "other", "expected_observation": "Other."})
            elif mutation == "duplicate":
                predictions[1]["hypothesis"] = IDS[0]
            elif mutation == "wrong_id":
                predictions[1]["hypothesis"] = "unknown"
            else:
                predictions[0], predictions[1] = predictions[1], predictions[0]
            with self.subTest(mutation=mutation), self.assertRaises(ProposalValidationError):
                self.parse(proposal)

    def test_differing_observations_are_required_beyond_different_hypothesis_ids(self):
        proposal = self.proposed()
        for index, prediction in enumerate(proposal["experiment"]["hypothesis_predictions"]):
            prediction["expected_observation"] = " Same outcome " if index else "Same outcome"
        with self.assertRaisesRegex(ProposalValidationError, "differing expected observations"):
            self.parse(proposal)

    def test_exact_fields_reject_model_selected_scope_or_claims(self):
        for field in self.proposed():
            proposal = self.proposed()
            del proposal[field]
            with self.subTest(missing=field), self.assertRaises(ProposalValidationError):
                self.parse(proposal)
        proposal = self.proposed()
        proposal["scientific_approval"] = True
        with self.assertRaises(ProposalValidationError):
            self.parse(proposal)
        for object_name, extra in (("experiment", {"scope": "wider"}),
                                   ("parameters", {"primary_metric": "chosen after result"}),
                                   ("hypothesis_predictions", {"observed_result": 0.8}),
                                   ("components", {"scientific_truth": 1.0})):
            proposal = self.proposed()
            target = proposal["experiment"]
            if object_name == "parameters":
                target = target["parameters"]
            elif object_name == "hypothesis_predictions":
                target = target["hypothesis_predictions"][0]
            elif object_name == "components":
                target = target["components"]
            target.update(extra)
            with self.subTest(extra=object_name), self.assertRaises(ProposalValidationError):
                self.parse(proposal)

    def test_domain_recipe_is_bounded_exploratory_and_has_supported_action(self):
        for field, value in (("recipe_id", "arbitrary_code"), ("mode", "confirmatory"),
                             ("action", "retry"), ("action", "replicate")):
            proposal = self.proposed()
            proposal["experiment"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ProposalValidationError):
                self.parse(proposal)
        for action in ("baseline", "discriminate", "ablate", "robustness"):
            proposal = self.proposed()
            proposal["experiment"]["action"] = action
            self.assertEqual(self.parse(proposal)["experiment"]["action"], action)

    def test_sample_size_and_parameter_types_have_no_bool_or_coercion(self):
        for value in (True, 31, 2049, 128.0, "128", None):
            proposal = self.proposed()
            proposal["experiment"]["parameters"]["n_samples"] = value
            with self.subTest(value=value), self.assertRaises(ProposalValidationError):
                self.parse(proposal)
        for size in (MIN_SAMPLES, MAX_SAMPLES):
            proposal = self.proposed()
            proposal["experiment"]["parameters"]["n_samples"] = size
            self.assertEqual(self.parse(proposal)["experiment"]["parameters"]["n_samples"], size)
        for field, value in (("assignment", "self_selected"), ("analysis", "model_chosen_p_value")):
            proposal = self.proposed()
            proposal["experiment"]["parameters"][field] = value
            with self.subTest(field=field), self.assertRaises(ProposalValidationError):
                self.parse(proposal)

    def test_components_are_exact_finite_unit_interval_values(self):
        for value in (True, False, -0.1, 1.1, "0.5", None, float("nan"), float("inf"), 10 ** 1000):
            proposal = self.proposed()
            proposal["experiment"]["components"][COMPONENTS[0]] = value
            with self.subTest(value=repr(value)[:30]), self.assertRaises(ProposalValidationError):
                self.parse(proposal)
        for value in (0, 1, 0.5):
            proposal = self.proposed()
            proposal["experiment"]["components"][COMPONENTS[0]] = value
            self.assertEqual(self.parse(proposal)["experiment"]["components"][COMPONENTS[0]], value)
        proposal = self.proposed()
        del proposal["experiment"]["components"][COMPONENTS[0]]
        with self.assertRaises(ProposalValidationError):
            self.parse(proposal)

    def test_text_bounds_and_limitations_are_enforced(self):
        for path in ("reason", "contrast", "rationale", "observation"):
            for bad in (None, "\u2003", "x" * (MAX_TEXT_LENGTH + 1)):
                proposal = self.proposed()
                if path == "reason":
                    proposal["reason"] = bad
                elif path == "contrast":
                    proposal["experiment"]["discriminating_contrast"] = bad
                elif path == "rationale":
                    proposal["experiment"]["rationale"] = bad
                else:
                    proposal["experiment"]["hypothesis_predictions"][0]["expected_observation"] = bad
                with self.subTest(path=path, bad=repr(bad)[:20]), self.assertRaises(ProposalValidationError):
                    self.parse(proposal)
        for limitations in (None, [], [" "], [None], ["x" * (MAX_LIMITATION_LENGTH + 1)],
                            ["bounded"] * (MAX_LIMITATIONS + 1)):
            with self.subTest(limitations=repr(limitations)[:20]), self.assertRaises(ProposalValidationError):
                self.parse(dict(self.proposed(), limitations=limitations))

    def test_strict_transport_rejects_duplicate_keys_nonfinite_and_wrappers(self):
        encoded = json.dumps(self.proposed()).encode("utf-8")
        for raw in (b"\xff", b"\xef\xbb\xbf" + encoded, b"```json\n" + encoded + b"\n```",
                    encoded + b" trailing", b"null", b"[]", b"{}"):
            with self.subTest(raw=raw[:20]), self.assertRaises(ProposalValidationError):
                validate_proposal(raw, IDS)
        duplicate = encoded.replace(b'"n_samples": 128', b'"n_samples": 64, "n_samples": 128')
        with self.assertRaisesRegex(ProposalValidationError, "duplicate JSON key"):
            validate_proposal(duplicate, IDS)
        for number in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            raw = encoded.replace(b'"discrimination": 0.5', b'"discrimination": ' + number)
            with self.subTest(number=number), self.assertRaisesRegex(ProposalValidationError, "nonfinite"):
                validate_proposal(raw, IDS)
        with self.assertRaises(ProposalValidationError):
            validate_proposal(encoded.decode(), IDS)

    def test_schema_separates_provider_shape_from_cross_field_validation(self):
        schema = STRUCTURED_OUTPUT_SCHEMA
        self.assertEqual(set(schema["required"]), set(self.proposed()))
        self.assertIs(schema["additionalProperties"], False)
        self.assertNotIn("allOf", schema)
        experiment = schema["properties"]["experiment"]["anyOf"][0]
        self.assertEqual(set(experiment["required"]), set(self.proposed()["experiment"]))
        self.assertIs(experiment["additionalProperties"], False)
        predictions = experiment["properties"]["hypothesis_predictions"]["items"]
        self.assertEqual(set(predictions["required"]), {"hypothesis", "expected_observation"})
        self.assertNotIn(IDS[0], json.dumps(schema))
        self.assertIn("allOf", PROPOSAL_SCHEMA)
        self.assertEqual(PROPOSAL_SCHEMA["properties"]["experiment"]["anyOf"][0]
                         ["properties"]["parameters"]["properties"]["n_samples"]["minimum"], MIN_SAMPLES)
        changed = deepcopy(schema)
        changed["properties"]["reason"]["type"] = "null"
        self.assertEqual(PROPOSAL_SCHEMA["properties"]["reason"]["type"], "string")

    def test_prompt_marks_synthetic_recipe_and_forbids_invented_results(self):
        self.assertEqual(PROMPT_VERSION, "experiment-proposal-v1")
        for phrase in ("frozen", '"abstained"', '"exploratory"', "synthetic fixture", "Do not invent"):
            self.assertIn(phrase, SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
