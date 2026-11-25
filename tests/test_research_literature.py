"""Literature retrieval preserves bytes and separates metadata from review."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from episteme.kernel import GateError
from episteme.research_literature import (
    project_literature_result, retrieve_literature, validate_literature_result,
)
from episteme.store import Store


SEARCH = {"kind": "crossref_search", "query": "Banknote authentication & variance", "rows": 2}
SOURCE = {"kind": "open_source", "url": "https://arxiv.org/html/2504.08066v1"}
FIXTURE = {"kind": "fixture_source", "fixture_id": "banknote-prior-art-v1"}


class ResearchLiteratureTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.addCleanup(self.store.close)

    def test_crossref_query_is_bounded_metadata_only(self):
        data = json.dumps({"message": {"items": [
            {"DOI": "10.1000/A1", "title": ["A title"], "author": [{"family": "Example"}],
             "issued": {"date-parts": [[2024]]}, "abstract": "Unselected"},
            {"DOI": "10.1000/a1", "title": ["Duplicate"]},
        ]}}).encode("utf-8")
        fetch = Mock(return_value=data)
        result = retrieve_literature(self.store, SEARCH, fetch=fetch)
        url = urlsplit(fetch.call_args.args[0])
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "api.crossref.org", "/works"))
        self.assertEqual(parse_qs(url.query)["query.bibliographic"], [SEARCH["query"]])
        self.assertEqual(parse_qs(url.query)["rows"], ["2"])
        self.assertEqual(result["content_kind"], "bibliographic_metadata")
        self.assertEqual(result["metadata"], [
            dict(doi="10.1000/a1", title="A title", authors=["Example"], year=2024),
        ])
        self.assertEqual(result, project_literature_result(self.store, result))
        self.assertEqual(self.store.read(result["artifact_digest"]), data)
        self.assertEqual(self.store.events(), [])
        forged = dict(result, metadata=[])
        with self.assertRaisesRegex(GateError, "differs from"):
            validate_literature_result(self.store, SEARCH, forged)

    def test_offline_fixture_uses_fixed_bytes(self):
        fetch = Mock(side_effect=AssertionError("fixture called external fetch"))
        result = retrieve_literature(self.store, FIXTURE, fetch=fetch)
        fetch.assert_not_called()
        self.assertEqual(result["content_kind"], "fixture_source")
        excerpt = project_literature_result(self.store, result)
        self.assertIn("10.24432/C55P57", excerpt["untrusted_source_text"])
        self.assertFalse(excerpt["checked_passage"])
        archived = dict(result, artifact_digest=self.store.put(b"forged prior art"))
        with self.assertRaisesRegex(GateError, "fixture bytes changed"):
            validate_literature_result(self.store, FIXTURE, archived)

    def test_only_explicit_source_is_called_and_projection_is_bounded(self):
        data = (b"<html><head><title>Ignored</title></head><body><script>Ignored</script>"
                b"<p>Source &amp; context</p><a href='https://example.invalid/'>link</a>"
                + b"x" * 7000 + b"</body></html>")
        fetch = Mock(return_value=data)
        result = retrieve_literature(self.store, SOURCE, fetch=fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args, (SOURCE["url"],))
        self.assertEqual(result["content_kind"], "source_bytes")
        excerpt = project_literature_result(self.store, result)
        self.assertTrue(excerpt["untrusted_source_text"].startswith("Source & context link"))
        self.assertNotIn("Ignored", excerpt["untrusted_source_text"])
        self.assertEqual(len(excerpt["untrusted_source_text"]), 4000)
        self.assertEqual(project_literature_result(self.store, result, start=5)["excerpt_offset"], 5)
        self.assertFalse(excerpt["checked_passage"])
        with self.assertRaisesRegex(GateError, "differs from"):
            validate_literature_result(self.store, dict(SOURCE, url="https://arxiv.org/html/2504.08066v2"),
                                       result)

    def test_rejects_unapproved_target_response_and_size(self):
        fetch = Mock(return_value=b"unused")
        for url in ("http://arxiv.org/html/2504.08066v1",
                    "https://arxiv.org@localhost/html/2504.08066v1",
                    "https://arxiv.org/html/2504.08066v1?redirect=https://example.invalid",
                    "https://arxiv.org/html/2504.08066", "https://example.invalid"):
            with self.subTest(url=url), self.assertRaisesRegex(GateError, "allowlist"):
                retrieve_literature(self.store, dict(SOURCE, url=url), fetch=fetch)
        with self.assertRaisesRegex(GateError, "result limit"):
            retrieve_literature(self.store, dict(SEARCH, rows=True), fetch=fetch)
        fetch.assert_not_called()
        with self.assertRaisesRegex(GateError, "must be UTF-8 text"):
            retrieve_literature(self.store, SOURCE, fetch=Mock(return_value=b"%PDF-1.7"))
        with self.assertRaisesRegex(GateError, "bounded bytes"):
            retrieve_literature(self.store, SOURCE, fetch=Mock(return_value=b"x" * (1024 * 1024 + 1)))
        with self.assertRaisesRegex(GateError, "invalid Crossref"):
            retrieve_literature(self.store, SEARCH, fetch=Mock(return_value=b"{}"))


if __name__ == "__main__":
    unittest.main()
