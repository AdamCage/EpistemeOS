# Audit of the Semantic Afterlife research harness

Audited 2026-09-06. Source: `C:/Projects/llm-semantic-afterlife`, branch `stage-2`, commit `4656b2ceda7bd86b6c213bc426d07e490d178405`. `git status --porcelain=v1 --untracked-files=all` was empty before and after inspection. Ignored research outputs exist locally. This was a read-only source and metadata audit: no experiments, paid requests, reproduction runs, gate executions, or source changes were performed. Statements about implementation are from code inspection, not a fresh test pass or independent scientific replication.

The source has an MIT license, with copyright attributed to the Semantic Afterlife contributors. Retain its notice if copying code. Links below point to this local checkout; the SHA above identifies the version inspected.

## Assessment

The strongest inheritance is the scientific operating discipline: explicit predictions, complete computational records, interpretable artifact bundles, negative findings retained, scientific review separated from execution, and replanning explained by decision records. The repository contains real implementations of logging, provenance, replay caching, analysis, and mechanical checks. It is a specific research project with agent instructions around a CLI, rather than a general autonomous research orchestrator.

The contract is stronger than its current mechanical enforcement. EpistemeOS should preserve the discipline while making its central guarantees executable. In particular, a passing legacy gate is neither proof of preregistration nor scientific approval. A cached replay is neither a fresh experiment nor an independent implementation.

## What exists and what to carry forward

| Capability | Inspected evidence | Reuse decision |
| --- | --- | --- |
| Scientific operating contract | [AGENTS.md](C:/Projects/llm-semantic-afterlife/AGENTS.md:32) requires run-backed numbers, resolved configs, provenance, self-contained artifacts, and explicit limits on interpretation. | Generalize into project policy and testable invariants. Keep domain methods in an adapter. |
| Executor / Supervisor separation | [roles and branches](C:/Projects/llm-semantic-afterlife/.cursor/rules/70-roles-and-branches.mdc:13) assigns execution and judgment separately. [Review instructions](C:/Projects/llm-semantic-afterlife/.cursor/skills/stage-review/SKILL.md:27) require reading PLAN before REPORT and seven methodological questions. | Reuse review rubric and ordering. Add explicit actor identity, scoped access, independent sessions, and immutable review targets. |
| Preregistered predictions and decision records | [Stage protocol](C:/Projects/llm-semantic-afterlife/.cursor/rules/40-stage-protocol.mdc:12), [S1 predictions](C:/Projects/llm-semantic-afterlife/docs/stages/stage-1/PLAN.md:160), [S1 scoring](C:/Projects/llm-semantic-afterlife/docs/stages/stage-1/REPORT.md:90). | Convert predictions, outcomes, budgets, amendments, and analysis choices into versioned records; render Markdown from them. |
| Results actually change the plan | [ADR-0008](C:/Projects/llm-semantic-afterlife/docs/decisions/ADR-0008-stage2-replan-after-convergence.md:24) explains why a plateau invalidates the planned half-life measurement and changes the next experiment to test a confound. | Use as the first real replanning fixture: competing explanations must produce a discriminating experiment, not merely a revised narrative. |
| Manifest and run lifecycle | [RunManifest](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/provenance.py:76) records config, Git state, environment, mode, seeds, endpoints, totals, timestamps, status, and output hashes. [RunContext](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/runctx.py:26) applies this to analyses and audits as well as generation. | Preserve the record fields; replace mutable lifecycle and weak identifiers with typed events, atomic finalization, explicit input links, and collision-safe IDs. |
| Config resolution | [Config loader](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/config.py:484) resolves defaults, validates typed configs, and hashes the result. | Reuse the pattern; generalize ExperimentSpec away from generator/window/seed matrices. |
| Replay and checkpoints | [ResponseCache](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/providers/cache.py:23) keys provider/path/canonical request; replay cache misses fail. [Trajectory resume](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/generation/trajectory.py:239) reconstructs state from step records. | Keep replay as a separate execution mode and checkpoint capability. Add durable write guarantees, immutable response identities, and explicit nondeterministic trials. |
| Artifact bundles | [FigureMeta](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/viz/export.py:25) includes run IDs, caption, limitations, Git/config hashes, units, and source data sidecars. | Generalize to Artifact/EvidenceBundle with content digests, input lineage, claim scope, and verifiable derivation. |
| Mechanical checks | [Gate registry](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/reporting/stage_review.py:555) runs 11 checks; [gate tests](C:/Projects/llm-semantic-afterlife/tests/test_stage_review.py:1) encode historical failure cases. | Reuse Check/Verdict/evidence/rationale interface and failure-case testing. Separate universal checks from domain checks. |
| Reproducibility levels | [Reproduction instructions](C:/Projects/llm-semantic-afterlife/.cursor/skills/reproduce-run/SKILL.md:12) distinguish cached replay, analysis rerun, and fresh statistical agreement. | Preserve these distinctions, add independent reimplementation as another dimension, and record achieved results rather than inferred labels. |
| Portable research state | [snapshot.py](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/snapshot.py:145) creates deterministic archives, verifies hashes, and checks extraction containment. | Generalize into evidence export/import with schema version and snapshot identity. Keep large objects outside ordinary Git history. |
| CI with no paid API | [CI](C:/Projects/llm-semantic-afterlife/.github/workflows/ci.yml:16) uses mock mode, Linux/Windows, Python 3.11/3.12, lint/types/tests, config expansion, and offline pipeline smoke. | Reuse the offline-first verification strategy and known-ground-truth fixtures. |

## Gaps that matter for a universal framework

### 1. Preregistration is a document convention

[check_plan_exists](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/reporting/stage_review.py:93) checks the presence of three strings: Exit criteria, Pre-registered predictions, Budget. It does not prove that the plan predates data acquisition, freeze its digest, or bind a run to a prediction revision. The source honestly records a criterion amendment 81 steps into a run and later explains the mistake in [S1 PLAN](C:/Projects/llm-semantic-afterlife/docs/stages/stage-1/PLAN.md:135).

Required replacement: a sealed protocol object containing predictions, primary estimands, analysis and exclusion rules, stopping rule, multiple-testing policy, expected outputs, and resource bounds. Every execution must reference its digest and a preceding seal event. An amendment creates a new version and records which data were already visible. Existing outcomes must retain the old target; exploratory results must remain distinguishable from confirmation.

### 2. Gate readiness allows incomplete review packages

[ready_for_review](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/reporting/stage_review.py:70) is simply the absence of FAIL. Incomplete runs produce WARN at [line 160](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/reporting/stage_review.py:160); a missing report produces SKIP at [line 349](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/reporting/stage_review.py:349). Integrity checks iterate only supplied hashes, so an empty integrity block passes with zero checked files at [line 187](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/reporting/stage_review.py:187). Artifact checks at [line 220](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/reporting/stage_review.py:220) verify sidecar presence, caption, and limitations, but do not resolve run references or hash artifact bytes against a registered derivation.

The report checks search for keywords or count quotes/fences, rather than verify each prediction and sampled output reference. Literature verification counts `LEAD` markers in one Markdown document; it does not resolve manuscript citations. CI executes `verify` in its smoke pipeline at [line 107](C:/Projects/llm-semantic-afterlife/.github/workflows/ci.yml:107), not the complete scientific stage gate.

Required replacement: transition-specific required checks, explicit applicability, complete planned-versus-observed accounting, typed prediction results, input/output integrity coverage, and gates bound to an exact state revision. Missing required evidence must block the relevant transition. Failed experiments can legitimately advance research after their outcome and missingness are recorded; they cannot silently support a successful-result claim. Mechanical completeness and scientific validity remain separate verdicts.

### 3. Reproduction is not automated by the advertised command

The reproduction skill says the command re-executes and writes `REPRODUCTION.md` at [line 29](C:/Projects/llm-semantic-afterlife/.cursor/skills/reproduce-run/SKILL.md:29). The actual [CLI implementation](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/cli.py:1776) reads the manifest and prints instructions for replay/analysis/fresh execution. It does not launch those runs or generate a comparison report. `afterlife compare` exists as a separate scientific-content diff, but that is not an end-to-end reproduction service.

Required replacement: executable reproduction recipes with a new run ID, frozen input/code/environment references, declared comparison criteria, stored deviations, and no network fallback during replay. A separate Replication role receives the sealed protocol, permitted raw data, and acceptance criteria without the executor's implementation or conclusions where feasible. Report isolation limits: different role names alone do not establish independence.

### 4. Budget checks are not concurrent reservations

[Ledger.reserve](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/ledger.py:109) checks recorded spend plus an estimate, but does not persist or increment a reserved balance. Concurrent requests can all pass before any is recorded. The lock is per Ledger object at [line 77](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/ledger.py:77); historical project spend is loaded once. Separate processes and separate runs therefore do not share a transactional spending view. The stage ceiling at [line 129](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/ledger.py:129) compares the single run's prospective total, not the aggregate of a stage's arms.

Required replacement: transactional reserve/commit/release operations across project/experiment/run scopes, idempotent request IDs, persisted outstanding reservations, unknown-cost reconciliation, and separate accounting for money, GPU time, wall time, and calls. This is prerequisite to parallel experiment search.

### 5. Provenance needs stronger source and artifact closure

[git_state](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/provenance.py:44) stores `git diff HEAD` when dirty. That captures tracked modifications but does not preserve untracked file contents. Source with untracked code therefore cannot be reconstructed from that record alone. [Manifest integrity patterns](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/provenance.py:129) intentionally cover config, requests, and data, excluding still-open events/logs; this avoids false alarms but leaves the event history outside the seal. Manifest writes at [line 123](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/provenance.py:123) and response cache writes are mutable file writes. [Run IDs](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/paths.py:25) have second-resolution timestamps plus an eight-character config suffix; simultaneous identical runs can collide.

Required replacement: a content-addressed source bundle or clean committed checkout plus complete permitted patch/untracked capture; dependency lock/container digest; separate execution, finalization, and seal events; durable immutable blobs; explicit parent input digests; and unique attempt IDs independent of config identity. A hash proves identity relative to its recorded value, not scientific truth or independence.

### 6. Persistent files are not a persistent scientific state model

The local checkout contains 52 ignored run manifests: 31 in s0 and 21 in s1. Of those, 43 report COMPLETED and 9 FAILED; 38 record dirty source state. These are manifest inventory counts, not judgments about result validity, and include historical/superseded runs. Full output hashes were not independently verified during this audit.

State is distributed among run manifests, per-step logs, artifact metadata, PLAN/REPORT/HANDOFF files, ADRs, and a shared snapshot release. Some analysis lineage exists through `source_run_id` in [cli.py](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/cli.py:799). There is no typed claim-evidence store, revision-aware invalidation, persistent scheduler, hypothesis tournament, or experiment tree implementation in the inspected source. The master plan's header still says S0 is in progress at [line 9](C:/Projects/llm-semantic-afterlife/docs/research-plan.md:9), while the checked-out branch opens stage 2: manually repeated status already drifts.

Required replacement: an append-only research event log with validated projections of hypotheses, experiments, runs, observations, claims, limitations, reviews, and decisions. Preserve contradictory evidence and superseded records. Derive human handoffs/status from this store; never make an LLM summary the source of truth. A dependency change must invalidate affected gate/review/export attestations.

### 7. Agent independence and publishing are procedural

The `.cursor` skills and rules assign human-readable roles. No runtime enforces identity, access separation, assignment state, review quorum, or approval-to-revision binding. `git ls-files '*REVIEW*' '*review*'` found the review skill, implementation, and tests, but no committed stage `REVIEW.md` at the audited SHA. This does not disprove external PR review; the saved conversation's PR #4 claim was not verified in this local audit.

[Branch discipline](C:/Projects/llm-semantic-afterlife/.cursor/rules/70-roles-and-branches.mdc:65) permits one stage branch at a time, which is useful for linear work but conflicts with concurrent experimental branches. [Paper rules](C:/Projects/llm-semantic-afterlife/.cursor/rules/50-paper.mdc:11) defer manuscript prose and require artifact/run comments; [report command](C:/Projects/llm-semantic-afterlife/src/semantic_afterlife/cli.py:1382) regenerates the artifact index. Neither is an automated approved-claim-to-paper pipeline.

Required replacement: separate research graph branches from Git implementation branches, scoped worker workspaces, explicit Executor/Replication/Reviewer assignments, review findings and dispositions tied to immutable target revisions, and manuscript exports restricted to eligible claim versions with cited evidence and limitations. Human submission/publication decisions remain an explicit boundary.

## Minimal import into EpistemeOS

Do not fork the entire application into the framework core. First implement an optional, read-only `afterlife` importer with these mappings:

| Legacy record | EpistemeOS record | Initial trust |
| --- | --- | --- |
| Git SHA + local source path | ExternalProjectSnapshot | Source observed; byte closure unverified until captured |
| PLAN + config + prediction rows | ImportedProtocol + Hypothesis/Prediction candidates | Historical, not automatically sealed preregistration |
| manifest + referenced raw files | Run + BlobReference + input lineage | Imported; verify hashes before evidence eligibility |
| completed/failed/superseded markers | RunOutcome / Supersession events | Preserve all outcomes and reasons |
| `.meta.json` + tidy data + figure | Artifact + EvidenceBundle candidate | Not automatically scientifically accepted |
| REPORT assertions and limitations | ClaimDraft + Limitations | Review required; preserve original wording/source location |
| ADRs | Decision + Replanning event | Historical decision, linked to cited evidence |
| Any externally recovered review | ReviewRecord | Require actor, target revision, findings, and provenance |

Import one small completed mock run and its artifact bundle first; preserve the original run ID in an external namespace. Then import one S1 prediction/negative result/ADR chain as a research-state example. Re-importing the same source snapshot must be idempotent. No importer should replay API calls, overwrite the source repository, infer an APPROVED verdict, or assign a pre-data timestamp based solely on prose.

A narrowly copied utility such as canonical hashing or deterministic snapshot creation can be adapted with attribution if useful. Keep analysis modules, plotting themes, provider pricing, tokenizer/window semantics, degeneracy gates, and fixed stage numbering in the Semantic Afterlife adapter. Reimplement budget reservation and protocol/review transitions in the new kernel because their concurrency and trust requirements differ fundamentally.

## First acceptance scenarios suggested by this audit

1. A run cannot start without a preceding sealed protocol revision; amending the protocol cannot change the meaning of an existing run.
2. Two concurrent workers cannot spend the same remaining budget or claim the same exclusive assignment.
3. A missing expected artifact, empty required hash inventory, corrupted blob, or stale review prevents evidence promotion.
4. Cached replay, same-code analysis rerun, fresh statistical replication, and independent reimplementation produce distinct records and claims.
5. Contradictory evidence and failed attempts survive replanning; the S1 plateau example leads to a mechanism-control proposal.
6. Executor cannot approve its own result; a reviewer receives a frozen target and structured findings are required before approval.
7. A paper export traces every promoted empirical claim to sealed evidence and current review, and records scope/limitations.
8. The entire small synthetic cycle runs offline and can be replayed from its persistent state after a process restart.
