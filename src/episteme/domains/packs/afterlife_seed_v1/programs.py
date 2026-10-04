"""Frozen runner programs of the historical Afterlife stop-rate pilot.

These bytes equal ``PRIMARY_SOURCE`` and ``REANALYSIS_SOURCE`` of the legacy
``episteme.domains.afterlife_seed`` module; a test compares them. Both read the
same already observed trajectory bytes; distinct files do not establish
independent authorship or isolation.
"""

PRIMARY_SOURCE = b'''"""Project one previously observed Afterlife trajectory."""
import base64
import hashlib
import json
from pathlib import Path
import sys


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def main():
    if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
        raise ValueError("expected input.dat --seed SLOT_INDEX")
    slot = int(sys.argv[3])
    bundle = json.loads(Path("input.dat").read_bytes())
    if (bundle.get("schema_version") != 1 or bundle.get("domain") != "afterlife_seed_v1"
            or len(bundle.get("trajectories", [])) != 9 or not 0 <= slot < 9):
        raise ValueError("invalid frozen trajectory bundle or slot")
    entry = bundle["trajectories"][slot]
    if entry["slot_index"] != slot:
        raise ValueError("trajectory slot mismatch")
    steps = base64.b64decode(entry["steps_b64"], validate=True)
    if hashlib.sha256(steps).hexdigest() != entry["steps_sha256"]:
        raise ValueError("trajectory bytes disagree with frozen digest")
    lines = steps.splitlines()
    if not lines:
        raise ValueError("empty trajectory")
    stopped = 0
    for index, line in enumerate(lines, 1):
        row = json.loads(line)
        if row["step"] != index:
            raise ValueError("noncontiguous steps")
        stopped += row["finish_reason"] not in (None, "length")
    rate = stopped / len(lines)
    record = entry["manifest"]
    if (record["n_steps"] != len(lines) or record["stop_events"] != stopped
            or record["stop_event_rate"] != round(rate, 4)):
        raise ValueError("observations disagree with historical manifest")
    raw = {"schema_version": 1, "domain": "afterlife_seed_v1",
           "manifest_sha256": bundle["manifest_sha256"], "trajectory": entry}
    Path("raw.json").write_bytes(encoded(raw))
    Path("metrics.json").write_bytes(encoded({"stop_event_rate": rate}))


if __name__ == "__main__":
    main()
'''


REANALYSIS_SOURCE = b'''"""Recount stops from the frozen primary raw observations."""
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


def main():
    if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
        raise ValueError("expected input.dat --seed SLOT_INDEX")
    slot = int(sys.argv[3])
    original = Path("input.dat").read_bytes()
    raw = json.loads(original)
    if (raw.get("schema_version") != 1 or raw.get("domain") != "afterlife_seed_v1"
            or not 0 <= slot < 9):
        raise ValueError("invalid primary raw observations")
    trajectory = raw["trajectory"]
    if trajectory["slot_index"] != slot:
        raise ValueError("raw observations belong to another slot")
    steps = base64.b64decode(trajectory["steps_b64"], validate=True)
    if hashlib.sha256(steps).hexdigest() != trajectory["steps_sha256"]:
        raise ValueError("raw trajectory digest mismatch")
    counts = Counter()
    for position, line in enumerate(steps.splitlines(), 1):
        row = json.loads(line)
        if row["step"] != position:
            raise ValueError("noncontiguous steps")
        counts[row["finish_reason"]] += 1
    total = sum(counts.values())
    if total == 0:
        raise ValueError("empty trajectory")
    stopped = total - counts[None] - counts["length"]
    rate = stopped / total
    record = trajectory["manifest"]
    if (record["n_steps"] != total or record["stop_events"] != stopped
            or record["stop_event_rate"] != round(rate, 4)):
        raise ValueError("independent recount disagrees with historical manifest")
    Path("raw.json").write_bytes(original)
    Path("metrics.json").write_text(json.dumps({"stop_event_rate": rate},
                                     sort_keys=True, separators=(",", ":"), allow_nan=False),
                                    encoding="utf-8")


if __name__ == "__main__":
    main()
'''
