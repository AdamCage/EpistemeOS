"""Exercise the import with synthetic raw-shaped rows and the existing pack."""

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from episteme.domains.packs.tabular_classification_v1.capture import read_directory
from episteme.domains.packs.tabular_classification_v1.model import parse_table
from episteme.store import Store


SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "banknote_authentication" / "prepare_capture.py"
SPEC = importlib.util.spec_from_file_location("prepare_banknote_capture", SCRIPT)
IMPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORTER)
PILOT_SPEC = importlib.util.spec_from_file_location("prepare_banknote_pilot",
                                                  SCRIPT.with_name("prepare_pilot.py"))
PILOT = importlib.util.module_from_spec(PILOT_SPEC)
with patch.dict(sys.modules, {"prepare_capture": IMPORTER}):
    PILOT_SPEC.loader.exec_module(PILOT)


class BanknoteCaptureTests(unittest.TestCase):
    def raw_fixture(self):
        return "".join(f"{index % 500}.12555,-{index % 75}.125,3,4,{index % 2}\n"
                       for index in range(1372)).encode("ascii")

    def test_pinned_offline_import_feeds_tabular_capture(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = self.raw_fixture()
            pinned = sha256(raw).hexdigest()
            source = root / "raw.txt"
            source.write_bytes(raw)
            destination = root / "capture"
            manifest_path = root / "manifest.json"
            result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(destination),
                                     "--sha256", pinned, "--manifest", str(manifest_path)],
                                    capture_output=True, text=True, check=True)
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest, json.loads(manifest_path.read_bytes()))
            self.assertEqual(manifest["source_sha256"], pinned)
            self.assertIn("10.24432/C55P57", manifest["citation"])
            self.assertEqual(manifest["license"], "CC-BY-4.0")
            captured = read_directory(destination)
            train = parse_table(captured.files["train.csv"])
            holdout = parse_table(captured.files["holdout.csv"])
            self.assertEqual(len(train) + len(holdout), 1372)
            self.assertFalse({row[:2] for row in train} & {row[:2] for row in holdout})
            files, expected = IMPORTER.prepare_capture(raw, pinned)
            self.assertEqual(captured.files, files)
            self.assertEqual(manifest, expected)
            state = root / "store"
            capture_cli = subprocess.run(
                [sys.executable, "-m", "episteme", "pack", "capture", "tabular_classification_v1",
                 "--source", str(destination), "--root", str(state)],
                capture_output=True, text=True, check=True)
            with Store(state) as store:
                self.assertTrue(store.read(json.loads(capture_cli.stdout)["capture"]))
                self.assertEqual(store.events(), [])

    def test_rejects_shifted_snapshot(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            IMPORTER.prepare_capture(self.raw_fixture(), "0" * 64)

    def test_pilot_locks_inputs_and_stays_unstarted(self):
        raw = self.raw_fixture()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "pilot"
            with self.assertRaisesRegex(ValueError, "does not match"):
                PILOT.prepare_pilot(destination, raw, model="fixture-model", reasoning_effort="low")
            self.assertFalse(destination.exists())
            with patch.object(PILOT, "SOURCE_SHA256", sha256(raw).hexdigest()):
                PILOT.prepare_pilot(destination, raw, model="fixture-model", reasoning_effort="low")
            config = json.loads((destination / "config.json").read_bytes())
            self.assertEqual(config["budget"], dict(stages=1, attempts=8, model_calls=24,
                                                   literature_calls=2, wall_seconds=1800,
                                                   operation_seconds=120))
            self.assertEqual([action["kind"] for action in config["literature"]],
                             ["open_source", "crossref_search"])
            self.assertEqual(config["candidates"][0]["phase"], "confirmatory")
            self.assertEqual(config["candidates"][0]["capture_source"], "capture")
            self.assertEqual((destination / PILOT.SOURCE_NAME).read_bytes(), raw)
            self.assertEqual({path.name for path in destination.iterdir()}, {
                "capture", "hypothesis.txt", "author-reply-template.txt", "config.json",
                "provenance.json", PILOT.SOURCE_NAME,
            })

    def test_grouped_pilot_keeps_first_source_row_before_splitting(self):
        group_count = 347
        lines = []
        for index in range(1372):
            occurrence, pair = divmod(index, group_count)
            lines.append(f"{pair}.12555,-{pair % 75}.125,{occurrence},4,{(pair + occurrence) % 2}\n")
        raw = "".join(lines).encode("ascii")
        pinned = sha256(raw).hexdigest()
        original, _ = IMPORTER.prepare_capture(raw, pinned)
        with TemporaryDirectory() as directory, patch.object(PILOT, "SOURCE_SHA256", pinned):
            destination = Path(directory) / "grouped"
            PILOT.prepare_pilot(destination, raw, model="fixture-model", reasoning_effort="low",
                                capture_version=2)
            config = json.loads((destination / "config.json").read_bytes())
            self.assertEqual(config["budget"]["model_calls"], 20)
            manifest = json.loads((destination / "provenance.json").read_bytes())
            grouped = read_directory(destination / "capture").files
            observed_pairs = {}
            for name, data in grouped.items():
                first_rows = {}
                raw_rows = parse_table(original[name])
                for row in raw_rows:
                    first_rows.setdefault(row[:2], row)
                self.assertEqual(parse_table(data), list(first_rows.values()))
                audit = manifest["grouping"]["split"][name]
                self.assertEqual(audit, dict(groups=len(first_rows), source_rows=len(raw_rows),
                                             dropped_rows=len(raw_rows) - len(first_rows)))
                observed_pairs[name] = set(first_rows)
            self.assertFalse(observed_pairs["train.csv"] & observed_pairs["holdout.csv"])
            self.assertEqual(manifest["grouping"]["groups"], group_count)
            self.assertEqual(manifest["grouping"]["dropped_rows"], 1372 - group_count)
            self.assertEqual(manifest["grouping"]["group_overlap"], 0)


if __name__ == "__main__":
    unittest.main()
