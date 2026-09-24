"""Prepare one bounded Codex experiment proposal; --execute contacts the model.

This is a synthetic planning fixture. It creates no run, claim or review and
does not assert that the proposed experiment is scientifically discriminating.
An already authenticated Codex CLI is required for --execute.
"""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from episteme.agent_controller import advance_agent
from episteme.agents import agent_state, freeze_recipe_binding
from episteme.codex_provider import freeze_provider
from episteme.commands import CommandService
from episteme.execution import freeze_environment
from episteme.search import COMPONENTS
from episteme.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.root.exists():
        parser.error("use a new directory; resume existing requests through episteme agent")
    with Store(args.root) as store:
        def command(action: str, **payload):
            return CommandService(store).execute(dict(context=dict(
                command_id=uuid4().hex, expected_revision=len(store.events()),
                actor="example-manager", role="planner", study_id="experiment-proposal-example",
                correlation_id="one-experiment-proposal", causation_id=None),
                request=dict(version=1, action=action, payload=payload)))

        scope = dict(domain="synthetic_causal", mode="proposal_only_no_observations")
        question = command("planning.question", study_id="experiment-proposal-example",
            statement="What synthetic test could distinguish no treatment effect from a positive effect?",
            objective="Specify a bounded exploratory contrast, without claiming observed results.",
            scope=scope,
            constraints=["Only a synthetic generator is available; no observations exist.",
                         "Propose one test without tools or external data."],
            stopping_criteria=["One proposal request; abstain if no useful bounded test exists."])
        null = command("kernel.hypothesis", statement="The synthetic treatment effect is zero.",
            prediction="A randomized treatment contrast is near zero.",
            falsifier="A stable positive randomized contrast.", scope=scope)
        mechanism = command("kernel.hypothesis", statement="The synthetic treatment effect is positive.",
            prediction="A randomized treatment contrast is positive.",
            falsifier="A stable zero randomized contrast.", scope=scope)
        explanations = command("planning.explanation_set", question=question,
            hypotheses=[null, mechanism],
            comparison_plan="Compare the effect estimate from a fixed synthetic randomized sample.")
        tree = command("search.register_tree", weights={key:1.0 for key in COMPONENTS},
            cost_weight=0.01, budget=4, cost_unit="enqueued_attempt", max_nodes=1,
            max_depth=0, max_width=1, max_selections=1)
        environment = freeze_environment(store)
        binding = freeze_recipe_binding(store,
            world=dict(treatment_effect=1.0, confounding_strength=0.5, noise_std=1.0),
            seeds=[7, 11], environment=environment)
        budget = command("agent.register_budget", study_id="experiment-proposal-example", max_calls=1)
        provider = freeze_provider(store, model=args.model, executable=args.executable)
        request = command("agent.request_experiment", budget=budget, explanation_set=explanations,
            tree=tree, recipe_binding=binding, assignee="example-experiment-planner",
            provider=provider, wall_seconds=120, max_output_bytes=1048576)
        print(json.dumps(dict(request=request, state="durably_queued",
                              live_model_call=args.execute)), flush=True)
        result = advance_agent(store, request) if args.execute else agent_state(store, request)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
