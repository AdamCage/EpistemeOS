"""Saved fixture histories keep their review bases, gates, Graph and exports.

The baseline was produced by the code recorded in expected.json. A failure
here means an old-history recipe changed; it needs an explicit decision, not a
regenerated baseline. Passing is mechanical compatibility, not scientific review.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import golden_support


class GoldenHistoryTests(unittest.TestCase):
    def setUp(self):
        self.expected = golden_support.read_json("expected.json")

    def test_saved_histories_keep_derived_hashes_and_replay(self):
        for name, expected in self.expected["stores"].items():
            with self.subTest(store=name), TemporaryDirectory(prefix="episteme-golden-") as temporary:
                store = golden_support.load_history(golden_support.read_json(f"{name}.json"),
                                                    Path(temporary) / "state")
                try:
                    self.assertEqual(golden_support.observe(store), expected)
                finally:
                    store.close()

    def test_legacy_synthetic_compiler_and_adapter_are_unchanged(self):
        self.assertEqual(golden_support.legacy_synthetic_recipes(),
                         self.expected["legacy_synthetic"])


if __name__ == "__main__":
    unittest.main()
