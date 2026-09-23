"""Prepare one real Codex proposal request; --execute explicitly contacts the model.

This is an educational synthetic question, not experimental evidence. There are
no runs, claims or reviewer approvals. Requires an already authenticated Codex
CLI; this script neither reads nor copies credentials.
"""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from episteme.agent_controller import advance_agent
from episteme.agents import agent_state
from episteme.codex_provider import freeze_provider
from episteme.commands import CommandService
from episteme.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.root.exists():
        parser.error("use a new directory; resume existing requests through episteme agent")
    with Store(args.root) as store:
        def command(action, **payload):
            return CommandService(store).execute(dict(context=dict(command_id=uuid4().hex,
                expected_revision=len(store.events()), actor="example-manager", role="planner",
                study_id="model-proposal-example", correlation_id="one-proposal", causation_id=None),
                request=dict(version=1, action=action, payload=payload)))
        question = command("planning.question", study_id="model-proposal-example",
            statement="How could we distinguish a real benefit from data leakage in a proposed synthetic regression benchmark?",
            objective="Propose falsifiable competing explanations and a small future discriminating experiment.",
            scope={"domain": "synthetic_regression", "mode": "proposal_only_no_observations"},
            constraints=["No observations have been collected; no effect has been established.",
                         "Do not run tools, experiments, search or commands; return a proposal only.",
                         "Future experiments would use independent generated train and test samples."],
            stopping_criteria=["One proposal request; retain uncertainty and abstain if necessary."])
        budget = command("agent.register_budget", study_id="model-proposal-example", max_calls=1)
        provider = freeze_provider(store, model=args.model, executable=args.executable)
        request = command("agent.request_hypotheses", budget=budget, question=question,
                          assignee="example-model-planner", provider=provider, wall_seconds=120,
                          max_output_bytes=1048576)
        print(json.dumps(dict(request=request, state="durably_queued", live_model_call=args.execute)), flush=True)
        result = advance_agent(store, request) if args.execute else agent_state(store, request)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
