"""Checks and the separate recount of the historical Afterlife stop rate.

The arithmetic equals the legacy ``afterlife_seed_batch_analysis`` adapter:
steps are split by bytes exactly as capture and both runner programs do, and
the 1e-12 tolerance concerns arithmetic only. This verifies byte identity and
counts on already observed data; it is not an independent data replication.
"""

import base64
import binascii
import math

from . import inventory


ENTRY_FIELDS = {"slot_index", "trajectory_id", "semantic_seed", "stochastic_seed",
                "steps_sha256", "steps_b64", "manifest"}


def number(value, label):
    inventory.require(type(value) in {int, float}, f"{label} must be numeric")
    try:
        converted = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be finite") from exc
    inventory.require(math.isfinite(converted), f"{label} must be finite")
    return converted


def captured(context, path):
    rows = [row for row in context.capture["inventory"] if row["path"] == path]
    inventory.require(len(rows) == 1, f"capture lacks {path}")
    return rows[0]["sha256"]


def bundle(context, cas):
    """The frozen input bundle, checked against the captured manifest bytes."""
    data = context.protocol["data"]
    inventory.require(context.execution_plan["input"]["sha256"] == data,
                      "protocol data differs from the pinned execution input")
    document = cas.json(data, "historical trajectory bundle")
    manifest_key = captured(context, inventory.MANIFEST_PATH)
    inventory.require(type(document) is dict
                      and set(document) == {"schema_version", "domain", "manifest_sha256", "trajectories"}
                      and document["schema_version"] == 1 and document["domain"] == inventory.DOMAIN_ID
                      and document["manifest_sha256"] == manifest_key
                      and type(document["trajectories"]) is list and len(document["trajectories"]) == 9,
                      "historical trajectory bundle has the wrong domain or roster")
    manifest = cas.json(manifest_key, "historical manifest")
    integrity, trajectories = manifest.get("integrity"), manifest.get("trajectories")
    inventory.require(type(integrity) is dict and type(trajectories) is dict
                      and {row["path"] for row in context.capture["inventory"]}
                      == set(integrity) | {inventory.MANIFEST_PATH},
                      "historical manifest differs from the captured inventory")
    entries = document["trajectories"]
    inventory.require([row.get("slot_index") for row in entries] == list(context.draft.roster)
                      == list(range(9)), "historical bundle roster differs from the slot indices")
    for row in entries:
        inventory.require(type(row) is dict and set(row) == ENTRY_FIELDS
                          and trajectories.get(row["trajectory_id"]) == row["manifest"]
                          and integrity.get(f"data/trajectories/{row['trajectory_id']}.steps.jsonl")
                          == row["steps_sha256"],
                          "historical trajectory differs from the captured manifest")
    return document, manifest


def steps(cas, entry):
    """Recount one frozen trajectory from its captured CAS bytes."""
    encoded = entry["steps_b64"]
    inventory.require(type(encoded) is str and 0 < len(encoded) <= 4 * 1024 * 1024,
                      "historical steps are missing or oversized")
    try:
        source = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid historical steps base64") from exc
    inventory.require(base64.b64encode(source).decode("ascii") == encoded
                      and 0 < len(source) <= inventory.MAX_STEPS_BYTES,
                      "noncanonical or oversized historical steps")
    inventory.require(inventory.sha256(source) == entry["steps_sha256"]
                      and cas.read(entry["steps_sha256"]) == source,
                      "historical steps differ from frozen CAS bytes")
    return inventory.steps(source, entry["trajectory_id"], entry["manifest"])


def metric(cas, key):
    document = cas.json(key, "historical metrics")
    inventory.require(type(document) is dict and set(document) == {inventory.METRIC},
                      "historical metrics require only stop_event_rate")
    value = number(document[inventory.METRIC], inventory.METRIC)
    inventory.require(0 <= value <= 1, "stop_event_rate is outside [0, 1]")
    return value


def raw(cas, key, manifest_key, entry):
    document = cas.json(key, "historical raw trajectory")
    inventory.require(type(document) is dict
                      and set(document) == {"schema_version", "domain", "manifest_sha256", "trajectory"}
                      and document["schema_version"] == 1 and document["domain"] == inventory.DOMAIN_ID
                      and document["manifest_sha256"] == manifest_key and document["trajectory"] == entry,
                      "observed raw trajectory differs from the frozen historical bundle")
    return document
