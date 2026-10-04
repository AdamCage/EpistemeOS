"""Static universality checks of ADR 0016 for the kernel/pack seam.

Kernel modules must not import concrete packs or branch on pack IDs. The
legacy exemptions below predate the contract; ADR 0016 plan step 9 (the model
path of ADR 0008) and the legacy CLI adapters are expected to remove them.
"""

import ast
import json
from pathlib import Path
import unittest

from episteme import commands
from episteme.domains import registry


SRC = Path(__file__).resolve().parents[1] / "src" / "episteme"
SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "command-v1.schema.json"
# Legacy domain wiring that the contract has not yet replaced; never extend it.
LEGACY_EXEMPT = {"agents.py", "experiment_proposals.py", "cli.py"}
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

    def test_published_command_schema_lists_exactly_the_runtime_actions(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        actions = schema["properties"]["request"]["properties"]["action"]["enum"]
        self.assertEqual(len(actions), len(set(actions)))
        self.assertEqual(set(actions), set(commands._ACTIONS))
        self.assertIn("pack.preregister", actions)
        self.assertIn("pack.analyse", actions)


if __name__ == "__main__":
    unittest.main()
