"""Optional GEPA search over bounded advice alternatives.

Development and validation cases are deliberately outside the closed paired
release suite. A GEPA result can only propose a candidate to that release gate.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .evolution import DockerBackend, EvolutionChannel, ROLES, _require, _text, validate_suite
from .store import canonical, digest


class _SilentLogger:
    def log(self, message: str) -> None:
        pass


def case_inputs(suite: dict[str, Any]) -> set[str]:
    return {digest(canonical(case["input"])) for case in suite["cases"]}


def freeze_search(*, options: dict[str, list[str]], training: dict[str, Any], validation: dict[str, Any],
                  backend: DockerBackend, max_evaluations: int, seed: int) -> dict[str, Any]:
    _require(type(backend) is DockerBackend, "GEPA evaluations require the Docker backend")
    _require(type(options) is dict and options and set(options) <= ROLES,
             "GEPA options must name supported instruction roles")
    for role, alternatives in options.items():
        _require(type(alternatives) is list and 1 <= len(alternatives) <= 8
                 and all(_text(item, 4096) for item in alternatives), f"invalid GEPA options for {role}")
    train, valid = validate_suite(training), validate_suite(validation)
    _require(not case_inputs(train) & case_inputs(valid), "GEPA training and validation inputs must be disjoint")
    initial_cost = 2 * len(valid["cases"]) + 2 * min(3, len(train["cases"]))
    _require(type(max_evaluations) is int and initial_cost <= max_evaluations <= 128
             and type(seed) is int and 0 <= seed < 2**32, "invalid GEPA evaluation budget or seed")
    return dict(options={role: list(texts) for role, texts in options.items()}, training=train, validation=valid,
                max_evaluations=max_evaluations, seed=seed)


def require_gepa() -> None:
    try:
        installed = version("gepa")
    except PackageNotFoundError as exc:
        raise ValueError("GEPA is optional; install epistemeos[evolution]") from exc
    _require(installed == "0.1.4", "GEPA adapter requires gepa==0.1.4")


def optimize_instructions(channel: EvolutionChannel, *, options: dict[str, list[str]],
                          training: dict[str, Any], validation: dict[str, Any],
                          backend: DockerBackend, max_evaluations: int = 24, seed: int = 0,
                          expected_parent: str | None = None) -> dict[str, Any]:
    """Select advice with GEPA 0.1.4 and submit it to the regular release channel.

    The proposal source is a finite list of instruction alternatives, so no
    reflection model, network credentials, closed answers, or Python changes
    are needed by the optimizer. Worker invocations run in Docker.
    """
    frozen = freeze_search(options=options, training=training, validation=validation,
                           backend=backend, max_evaluations=max_evaluations, seed=seed)
    train, valid = frozen["training"], frozen["validation"]
    training_cases, validation_cases = train["cases"], valid["cases"]
    minibatch = min(3, len(training_cases))
    iteration_limit = 2 * minibatch + len(validation_cases)
    require_gepa()
    from gepa import optimize
    from gepa.core.adapter import EvaluationBatch

    base = channel.pin()
    _require(expected_parent is None or base["release"] == expected_parent, "optimization parent is stale")
    original = base["profile"]["role_instructions"]
    seed_candidate = {role: original.get(role, "") for role in sorted(options)}
    mutations = [(role, advice) for role in sorted(options)
                 for advice in dict.fromkeys(options[role]) if advice != seed_candidate[role]]
    _require(bool(mutations), "GEPA alternatives do not change the active instructions")
    observations: list[dict[str, Any]] = []

    def advice(candidate: dict[str, str]) -> dict[str, str]:
        _require(set(candidate) == set(seed_candidate), "GEPA tried to change instruction components")
        return {role: text for role, text in {**original, **candidate}.items() if text}

    class Adapter:
        # GEPA 0.1.4 consults this optional hook directly when a custom
        # proposer is supplied; None delegates to that proposer.
        propose_new_texts = None

        def evaluate(self, batch, candidate, capture_traces=False):
            _require(len(observations) + len(batch) <= max_evaluations, "GEPA exceeded its invocation budget")
            profile = dict(role_instructions=advice(candidate))
            pin = dict(base, profile=profile)
            outputs, scores = [], []
            for case in batch:
                output = backend.invoke("evaluate", case["input"], pin)
                score = float(canonical(output) == canonical(case["expected"]))
                outputs.append(output)
                scores.append(score)
                observations.append(dict(case=case["id"], profile=profile, output=output, score=score))
            return EvaluationBatch(outputs=outputs, scores=scores,
                                   trajectories=list(zip(outputs, scores)) if capture_traces else None)

        def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
            return {role: [{"matched_expected": bool(score)} for score in eval_batch.scores]
                    for role in components_to_update}

    def propose(candidate, reflective_dataset, components_to_update):
        role, instruction = mutations.pop(0)
        _require(role in components_to_update, "GEPA did not select the proposed instruction component")
        return {role: instruction}

    def stop(state):
        return not mutations or len(observations) + iteration_limit > max_evaluations

    result = optimize(seed_candidate=seed_candidate, trainset=training_cases, valset=validation_cases,
                      adapter=Adapter(), custom_candidate_proposer=propose, module_selector="all",
                      reflection_minibatch_size=minibatch, skip_perfect_score=False,
                      max_metric_calls=max_evaluations, stop_callbacks=[stop], logger=_SilentLogger(), seed=seed)
    selected = advice(result.best_candidate)
    # The release channel checks that later paired evaluation has no overlap
    # with these optimization inputs. Candidate recommendations never see answers.
    trace = channel.store.put_json(dict(adapter="gepa==0.1.4", seed=seed, max_evaluations=max_evaluations,
                                       case_inputs=sorted(case_inputs(train) | case_inputs(valid)), backend=backend.descriptor,
                                       training=channel.store.put_json(train), validation=channel.store.put_json(valid),
                                       options=options, observations=observations))
    if selected == original:
        return dict(status="no_improvement", evaluations=len(observations), candidate=None, trace=trace)
    candidate = channel._propose(profile=dict(role_instructions=selected), patch=None,
                                 provenance="GEPA selection on development and validation cases",
                                 expected_parent=base["release"], optimization=trace)
    return dict(status="proposed", evaluations=len(observations), candidate=candidate, trace=trace)
