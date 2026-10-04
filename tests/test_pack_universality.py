"""Static universality checks of ADR 0016 for the kernel/pack seam.

Kernel modules must not import concrete packs or branch on pack IDs.
``experiment_proposals.py`` still names the frozen experiment-proposal-v1
recipe. ``legacy_experiment.py`` is the compiler for agent schema 2. The model
path modules must not import a concrete pack.
"""

import ast
import json
from pathlib import Path
import unittest

from episteme import commands
from episteme.domains import registry


SRC = Path(__file__).resolve().parents[1] / "src" / "episteme"
SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "command-v1.schema.json"
# experiment_proposals.py is the frozen experiment-proposal-v1 schema: its text
# names one recipe and must stay readable. legacy_experiment.py is the frozen
# compiler for agent_request schema 2. The generalized path does not use either
# to import a pack. cli.py no longer names a pack.
LEGACY_EXEMPT = {"experiment_proposals.py", "legacy_experiment.py"}
PROPOSAL_MODULES = ("agents.py", "experiment_proposals.py", "experiment_proposals_v2.py",
                    "pack_proposals.py")
DOMAIN_IDS = {"synthetic_causal_v1", "afterlife_seed_v1"}


class PackUniversalityTests(unittest.TestCase):
    def test_kernel_modules_neither_import_packs_nor_name_pack_ids(self):
        names = set(registry.PACKS) | DOMAIN_IDS
        for path in sorted(SRC.glob("*.py")):
            if path.name in LEGACY_EXEMPT:
                continue
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_bytes())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        module = "." * node.level + (node.module or "")
                        self.assertNotIn("packs", module.split("."), module)
                        self.assertFalse(module.lstrip(".").startswith("domains.")
                                         and module.lstrip(".") not in {"domains.api", "domains.registry",
                                                                        "domains.afterlife"}, module)
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertNotIn("packs", alias.name.split("."), alias.name)
                    elif isinstance(node, ast.Constant) and type(node.value) is str:
                        self.assertNotIn(node.value, names)

    def test_model_proposal_modules_do_not_import_a_concrete_pack(self):
        allowed = {"domains.api", "domains.registry", "domains.afterlife"}
        for name in PROPOSAL_MODULES:
            path = SRC / name
            tree = ast.parse(path.read_bytes())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.ImportFrom):
                    modules.append("." * node.level + (node.module or ""))
                elif isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                for module in modules:
                    parts = module.lstrip(".").split(".")
                    self.assertNotIn("packs", parts, module)
                    self.assertNotIn("synthetic_causal", parts, module)
                    bare = module.lstrip(".")
                    self.assertFalse(bare.startswith("domains.") and bare not in allowed, module)

    def test_published_command_schema_lists_exactly_the_runtime_actions(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        actions = schema["properties"]["request"]["properties"]["action"]["enum"]
        self.assertEqual(len(actions), len(set(actions)))
        self.assertEqual(set(actions), set(commands._ACTIONS))
        self.assertIn("pack.preregister", actions)
        self.assertIn("pack.analyse", actions)


if __name__ == "__main__":
    unittest.main()
