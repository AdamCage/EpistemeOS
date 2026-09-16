"""Pure claim-link contracts; fabricated IDs and hashes are explicit fixtures."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import unittest

from episteme.claims import ClaimLink, ClaimLinkError


def claim_link_fixture():
    return dict(schema_version=1, source="claim-fixture-new", target="claim-fixture-old",
                relation="contradicts", rationale="Synthetic conflicting estimates, not verified science",
                source_hash="a" * 64, target_hash="b" * 64,
                source_basis="c" * 64, target_basis="d" * 64)


class ClaimLinkTests(unittest.TestCase):
    def test_round_trip_is_immutable_and_detached(self):
        source = claim_link_fixture()
        expected = deepcopy(source)
        link = ClaimLink.from_dict(source)
        self.assertEqual(link.to_dict(), expected)
        self.assertEqual(ClaimLink.from_dict(json.loads(json.dumps(link.to_dict()))), link)
        source["rationale"] = "Changed caller input"
        exported = link.to_dict()
        exported["source_basis"] = "0" * 64
        self.assertEqual(link.to_dict(), expected)
        with self.assertRaises(FrozenInstanceError):
            link.target = "changed"

    def test_relation_vocabulary_preserves_the_declared_direction(self):
        link = ClaimLink.from_dict(claim_link_fixture())
        for relation in ("supports", "contradicts", "limits", "supersedes"):
            with self.subTest(relation=relation):
                changed = replace(link, relation=relation)
                self.assertEqual((changed.source, changed.target, changed.relation),
                                 ("claim-fixture-new", "claim-fixture-old", relation))
        for invalid in ("approve", "contradiction", "", None, ["supports"]):
            with self.subTest(invalid=invalid), self.assertRaises(ClaimLinkError):
                replace(link, relation=invalid)

    def test_missing_unknown_and_nonobject_payloads_fail_closed(self):
        source = claim_link_fixture()
        for key in source:
            incomplete = dict(source)
            del incomplete[key]
            with self.subTest(key=key), self.assertRaisesRegex(ClaimLinkError, "missing required"):
                ClaimLink.from_dict(incomplete)
        with self.assertRaisesRegex(ClaimLinkError, "unknown fields"):
            ClaimLink.from_dict(dict(source, confidence=1.0))
        for invalid in (None, [], "claim-id"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ClaimLinkError, "must be an object"):
                ClaimLink.from_dict(invalid)

    def test_versions_and_string_fields_require_exact_types_and_content(self):
        link = ClaimLink.from_dict(claim_link_fixture())
        for version in (True, 1.0, "1", 2, None):
            with self.subTest(version=version), self.assertRaisesRegex(ClaimLinkError, "schema_version"):
                replace(link, schema_version=version)
        for field, value in (("source", None), ("source", " \t"), ("target", 1),
                             ("target", ""), ("rationale", False), ("rationale", "\n")):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ClaimLinkError, field):
                replace(link, **{field: value})

    def test_self_links_are_rejected_for_all_relations(self):
        source = claim_link_fixture()
        source["target"] = source["source"]
        for relation in ("supports", "contradicts", "limits", "supersedes"):
            source["relation"] = relation
            with self.subTest(relation=relation), self.assertRaisesRegex(ClaimLinkError, "distinct claim IDs"):
                ClaimLink.from_dict(source)

    def test_each_revision_and_basis_hash_requires_a_canonical_digest(self):
        link = ClaimLink.from_dict(claim_link_fixture())
        for field in ("source_hash", "target_hash", "source_basis", "target_basis"):
            for invalid in ("a" * 63, "A" * 64, "g" * 64, "a" * 64 + "\n", float("nan"), None):
                with self.subTest(field=field, invalid=invalid), self.assertRaisesRegex(ClaimLinkError, field):
                    replace(link, **{field: invalid})

    def test_public_schema_fields_and_fixture_example_match_python_contract(self):
        path = Path(__file__).resolve().parents[1] / "schemas" / "claim-link-v1.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(set(schema["required"]), set(ClaimLink.__dataclass_fields__))
        self.assertEqual(set(schema["properties"]), set(schema["required"]))
        self.assertFalse(schema["additionalProperties"])
        self.assertTrue(schema["examples"])
        for example in schema["examples"]:
            self.assertIn("fixture", example["rationale"])
            self.assertEqual(ClaimLink.from_dict(example).to_dict(), example)


if __name__ == "__main__":
    unittest.main()
