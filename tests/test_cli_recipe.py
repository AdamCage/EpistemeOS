"""The host recipe CLI freezes inputs without creating research findings."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from episteme.execution import freeze_environment
from episteme.store import Store


class CliRecipeTests(unittest.TestCase):
    def test_freeze_recipe_from_host_file_without_model_call_or_events(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            recipe_file = Path(directory) / "recipe.json"
            with Store(root) as store:
                environment = freeze_environment(store)
                before = store.export()
            recipe_file.write_text(json.dumps(dict(
                world=dict(treatment_effect=1.0, confounding_strength=2.0, noise_std=0.5),
                seeds=[11, 12], environment=environment, replication_tolerance=0.1)),
                encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "episteme", "agent", "recipe", "--input",
                                     str(recipe_file), "--root", str(root)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            binding = json.loads(result.stdout)["recipe_binding"]
            with Store(root, read_only=True) as store:
                frozen = json.loads(store.read(binding))
                self.assertEqual(frozen["seeds"], [11, 12])
                self.assertEqual(frozen["world"]["treatment_effect"], 1.0)
                self.assertEqual(store.export(), before)
                self.assertEqual(store.events(), [])


if __name__ == "__main__":
    unittest.main()
