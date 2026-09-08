# EpistemeOS contributor contract

Read `docs/architecture.md` and `docs/mvp-plan.md` before changing research workflow semantics.
The user's working language is Russian. Code and API identifiers are English; design documents are Russian.

- Keep scientific validity separate from mechanical validity, tournament priority and execution success.
- Preserve failed attempts, negative findings, original preregistrations and competing explanations.
- Never turn missing evidence, missing replication or unresolved scientific findings into success.
- Bind decisions to immutable evidence revisions; validate again at a persisted transition.
- Distinguish same-code replay, fresh-seed repetition, independent reanalysis and new-data replication.
- State actual actor/context/OS isolation guarantees. Different role labels alone do not prove independence.
- Synthetic fixtures are fixtures. Do not manufacture scientific reviewer approvals for demo output.
- Put domain-specific metrics and scientific assumptions in adapters, not the universal kernel.
- No expensive experiments, external messages or publication is implied by unit tests or local demos.

Verification for semantic changes: `python -m unittest discover -s tests -v` after installing the package
or setting `PYTHONPATH=src`. Run relevant CLI integration checks when changing storage or interfaces.
Keep docs honest about what is implemented, tested locally, planned, or externally unverified.
