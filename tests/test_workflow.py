"""Real offline search-to-evidence path without fabricated scientific approval."""

import tempfile
import unittest
from pathlib import Path

from episteme.demo import run_demo
from episteme.kernel import Actor, Kernel
from episteme.search import Search
from episteme.store import Store


class WorkflowTests(unittest.TestCase):
    def test_search_demo_preserves_alternative_and_stops_at_real_review_boundary(self):
        with tempfile.TemporaryDirectory(prefix="episteme-search-flow-") as directory:
            root = Path(directory) / "study"
            result = run_demo(root, with_search=True)
            self.assertEqual(result["next_action"]["action"], "scientific_review")
            self.assertTrue(result["claims"][0]["gate"]["passed"])
            tree = result["search"]["tree"]
            self.assertEqual(tree["spent"], "6")
            self.assertEqual(tree["reserved"], "0")
            self.assertEqual(tree["remaining"], "0")
            self.assertEqual(sorted(n["state"] for n in tree["nodes"].values()), ["completed", "pending"])
            self.assertEqual(result["search"]["tournament"]["meaning"], "research_priority_only")
            with Store(root, read_only=True) as store:
                history = store.events()
                self.assertFalse(any(e["kind"] in {"review", "paper"} for e in history))
                selections = [e for e in history if e["kind"] == "search_selection"]
                runs = [e for e in history if e["kind"] == "run"]
                ballots = [e for e in history if e["kind"] == "tournament_ballot"]
                self.assertLess(max(e["seq"] for e in ballots), selections[0]["seq"])
                self.assertLess(selections[0]["seq"], min(e["seq"] for e in runs))
                self.assertEqual(selections[-1]["payload"]["reason"], "budget_exhausted")
                self.assertTrue(any(x["reason"] == "insufficient_budget" for x in selections[-1]["payload"]["frontier"]))
                search = Search(store, Actor("observer", "observer"))
                self.assertEqual(search.tree_state(tree["tree"]), tree)
                self.assertEqual(search.ranking(result["search"]["tournament"]["tournament"]),
                                 result["search"]["tournament"])
                self.assertEqual(Kernel(store, Actor("observer", "observer")).next_action(result["claim"]),
                                 result["next_action"])


if __name__ == "__main__":
    unittest.main()
