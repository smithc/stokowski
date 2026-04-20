---
date: 2026-04-19
topic: scheduled-jobs
focus: First-class "scheduled job" style workflows in the Stokowski orchestrator (e.g. `/compound-refresh` on a cadence, background operational analysis of workflow prompts)
---

# Ideation: First-Class Scheduled Jobs in Stokowski

## Codebase Context

Stokowski today is 100% Linear-driven:

- Single poll loop in `orchestrator.py` (`_tick` = reconcile → fetch → dispatch), ~30s interval, `workflow.yaml` hot-reloaded each tick.
- State machine in `config.py`: every state is `agent | gate | terminal` and every state requires a `linear_state`. Tracking persists as hidden-JSON Linear comments (`tracking.py`) so crash recovery means re-polling Linear.
- No persistent database. State lives in-memory; workspace directories act as durable state. Per-issue tracking dicts in `Orchestrator.__init__` must be mirrored in `_cleanup_issue_state()` — a documented footgun in `CLAUDE.md`.
- Retry queue uses `asyncio.call_later` with exponential backoff. Semantically "retry failed work," not "next fire time."
- Three-layer prompt: `prompts/global.md` + state's `prompt` file + auto-injected lifecycle section (`prompt.build_lifecycle_section`).
- Workspaces keyed by sanitized issue identifier; ephemeral per issue; shell hooks run on create/remove/stage-enter.
- Concurrency caps are per-state (`max_concurrent_agents_by_state`). No bucket for non-state work.
- Optional FastAPI dashboard in `web.py` (`POST /api/v1/refresh` already exists as a precedent for external triggers).
- Existing adjacent work: `docs/plans/2026-03-24-005-feat-multi-workflow-support-plan.md` (the natural integration seam), `docs/plans/2026-03-24-003-feat-agent-guardrails-and-cancel-workflow-plan.md` (cancellation discipline), `docs/plans/2026-03-24-004-feat-agent-run-log-retention-plan.md` (logging infra scheduled jobs should also use).
- `docs/solutions/` does not exist — there are no prior learnings to consult on this topic.
- Deployment target: hosted Fargate MVP for 3–5 internal Enterprise teams. EventBridge is already available infrastructure. MVP scope discipline applies.

What's missing for scheduled work:
1. No time-triggered or cadence-driven dispatch — every dispatch is demand-driven from Linear.
2. The state machine can't express "wake later" or "autonomous entry" — all entries require a Linear issue.
3. No tracking durability for non-Linear work (no issue to comment on).
4. Workspaces are ephemeral-per-issue — no story for persistent or shared workspaces across recurring runs.
5. No concurrency or overlap semantics for scheduled work.

---

## Ranked Ideas

### 1. Synthetic-issue scheduler (minimal first-class)
**Description:** Add a cron-aware source inside the orchestrator that fires on schedule and produces synthetic `Issue` objects (identifier like `CRON-compound-refresh-20260419T0800Z`). The synthetic issue enters the existing `_dispatch` path unchanged — same `RunAttempt`, same workspace logic, same retry queue, same `_run_worker`. Because no Linear issue exists, state tracking moves to a local JSONL ledger (`{log_dir}/_schedules/<name>.jsonl`) that `tracking.py` grows parallel `make_schedule_tracking` / `parse_schedule_tracking` functions for. Minimal knobs: `cron` (or `every`), `prompt`, `runner`. Overlap policy defaults to `skip` (drop fire if previous still running). Workspace mode defaults to `ephemeral`.
**Rationale:** Maximum reuse with minimum new surface. Operators debug scheduled runs the same way they debug issue runs (same logs, same dashboard row, same workspace layout). One new concept (synthetic fire source), zero changes to dispatch/runner/prompts beyond tolerating missing Linear-shaped fields in `build_lifecycle_section`.
**Downsides:** "Synthetic issue" is a leaky abstraction — lifecycle prompt must gracefully handle missing `issue_url`, `issue_branch`, `issue_labels`. Tracking lives in two places now (Linear comments + JSONL). Recurring runs with accumulating state (e.g. `/compound-refresh` writing to `.context/`) aren't solved by ephemeral workspaces — needs at minimum a `workspace_mode: persistent` knob.
**Confidence:** 75% | **Complexity:** Medium
**Status:** Unexplored

### 2. `schedules:` block + dedicated cron wheel (full first-class)
**Description:** `schedules:` at the workflow.yaml root alongside `states:`. Each entry carries `cron`/`interval`, `prompt`, `runner`, `overlap_policy` (`skip | queue | cancel_previous | parallel`), `workspace_mode` (`ephemeral | persistent | shared:<key>`), `on_missed` (`skip | run_once | run_all`), `max_runtime_ms`. Orchestrator grows a `_schedule_wheel` evaluated at the top of `_tick()` — explicitly **not** fused with the retry queue, because retries and next-fire-times are semantically different. Adds CLI verbs (`stokowski schedule list`, `stokowski schedule run <name>`), a "Schedules" panel in `web.py`, and a schedule JSONL ledger for fire history + crash recovery.
**Rationale:** "Full first-class support" taken literally. Every operator affordance — run-now, observability, overlap semantics, missed-fire policy, workspace reuse — has a designed home. Hot-reload already works because config is re-parsed each tick.
**Downsides:** Largest new surface area. Overlap/missed-fire/workspace-mode knobs are easy to over-design — for 3–5 internal teams, some may be YAGNI. Must add a parallel cleanup discipline to `_cleanup_issue_state()` (schedule-keyed) or repeat the documented footgun. Validation grows.
**Confidence:** 80% | **Complexity:** High
**Status:** Unexplored

### 3. Gate-with-resolver generalization — the state machine *is* the scheduler
**Description:** Gates today wait on one resolver: human Linear state change. Generalize `StateConfig` gate fields with `resolver: human | clock | artifact_changed | webhook | workflow_completed`. "Scheduled" becomes `resolver: clock` with `after: 24h` or `at: <cron>`. `/compound-refresh` becomes a two-state loop: `refresh` (agent) → `sleep` (gate, resolver=clock, after=24h) → back to `refresh`. `_reconcile()` grows a resolver dispatch table; the existing `human` path is one branch. `tracking.make_gate_comment` includes resolver metadata for recovery.
**Rationale:** Collapses scheduling, webhooks, artifact dependencies, and human approval into one abstraction. Every future async trigger plugs in as a resolver. Composes beautifully with the multi-workflow plan. No new primitive in the operator's mental model.
**Downsides:** Puts load on the gate abstraction it wasn't designed for. Gates today assume a Linear state to advance *to*; recurring workflows don't terminate and break `validate_config`'s terminal-state invariant. `_pending_gates` keying + reconciliation query path assume a Linear issue id. Likely significant surgery in `orchestrator.py` and `tracking.py`.
**Confidence:** 60% | **Complexity:** High
**Status:** Explored (brainstormed 2026-04-19)

### 4. HTTP trigger + external scheduler
**Description:** Stokowski ships zero scheduling logic. Add one route to the existing `web.py`: `POST /api/v1/trigger/<workflow>` that synthesizes an `Issue` in the workflow's entry state. Operators use EventBridge (Fargate), k8s CronJobs, or local `cron`/`launchd` to hit it. Extend with signal triggers (`workflow.yaml committed`, `run_failed_3x`) emitted from `_on_worker_exit` callbacks.
**Rationale:** Matches the AWS Fargate MVP story — EventBridge already provides cron, retries, and observability we'd otherwise reinvent. MVP scope discipline applied literally. Fastest path to "scheduled `/compound-refresh`."
**Downsides:** Pushes scheduled-work observability into each operator's external tooling — conflicts with the "one pane of glass" instinct that motivates the dashboard. Local-dev operators now need a second tool to test recurring jobs. Scheduled run logs don't live next to issue run logs. Multi-tenant hosting has to multiplex tenants' EventBridge rules or accept per-tenant config sprawl.
**Confidence:** 70% | **Complexity:** Low
**Status:** Unexplored

### 5. Agent-drafted schedules (`<!-- schedule:next at=... -->`)
**Description:** Orthogonal composition layer. Agents emit a next-wake directive at turn end, parsed exactly like the existing `<!-- transition:cancel -->`. Stokowski parks the (synthetic or real) issue and wakes it at the stated timestamp. An analysis agent can say "nothing changed, check again in 7d" or "saw drift, check in 4h." Implemented in `runner._process_event`; parked state tracked in a new `_pending_wake: dict[str, datetime]` (with the obligatory `_cleanup_issue_state` entry).
**Rationale:** Shifts cadence from config to conversation. Directly serves the operational-analysis use case — the agent knows better than the operator when to re-run. Adaptive cadence without heuristic inference.
**Downsides:** Non-deterministic schedules are harder to reason about and test. Needs guards (max delay, max re-wake count) to prevent "check back in 10 years" or infinite self-rescheduling. Pure composition — still needs a base mechanism (1–4) to park work against.
**Confidence:** 65% | **Complexity:** Medium
**Status:** Unexplored

### 6. Invariants, not schedules
**Description:** Operators declare desired state rather than cadence, e.g., `every_prompt_file_reviewed_within: 30d`, `no_workflow_yaml_commit_unscanned`, `no_stage_has_stall_rate_above: 0.2`. A checker runs in `_tick()`; when an invariant is violated, it synthesizes an issue targeting that specific violation. Self-healing — fires only when the invariant is actually broken, stops firing once fixed.
**Rationale:** Inverts the premise. No redundant runs, no thundering herd, no missed-fire replay problem. Maps naturally onto the "operational analysis" use case the user cited — prompt freshness is genuinely an invariant, not a cadence.
**Downsides:** Requires a language for expressing invariants (new config shape, new evaluator). Works poorly for cases where cadence is genuinely what's wanted (weekly digest emails). Probably an *additional* feature atop a real scheduler, not a replacement — pushes into brainstorm-variant territory for this pass.
**Confidence:** 55% | **Complexity:** High
**Status:** Unexplored

---

## Capabilities Unlocked (future-use, not mechanism choices)

Once any mechanism above ships, these become possible. Recorded here so they're not lost, but not scored in this ideation — each is a future brainstorm once the mechanism is chosen.

| Capability | Feeds |
|------------|-------|
| Nightly prompt autopsy (read `.ndjson` logs, propose prompt PRs) | Prompt quality compounding |
| Workflow lint bot (study tracking comments, propose `workflow.yaml` edits) | Config quality compounding |
| Solutions librarian (auto-populate `docs/solutions/`) | Institutional memory compounding |
| Tracking-comment ledger compaction | Lifecycle token cost |
| Prompt A/B split scheduler | Measured prompt improvement |
| Synthetic canary issues | Regression detection vs model/config changes |
| Token budget governor | Cost oversight on hosted Fargate |
| Auto-tuner for concurrency / stall timeouts / backoff | Self-tuning platform |
| Workflow graduation curator | Multi-workflow sprawl management |
| Cross-workflow refactor dispatcher (meta-engineer workflow improves others) | Breaks narcissistic self-improvement loop |
| Rework-context distillation | Token cost on worst-offender issues |
| Agent run log → learnings → prompt-improvement PR | Full ce-compound loop |

---

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Linear recurring-issue templates | Not a well-supported Linear primitive; relies on external tool doing the job poorly |
| 2 | Scheduled jobs *replace* the retry loop (unified future-work queue) | Conflates "recover from failure" with "next fire"; semantics diverge enough that unification costs clarity |
| 3 | Output-stability inferred cadence (auto-backoff from unchanged findings) | Premature optimization; needs a real scheduler to tune, not a replacement for one |
| 4 | Self-observing job proposer (system suggests schedules from usage) | Strong idea but requires scheduling to already exist; it's a capability, not a mechanism |
| 5 | Continuous-agent-runtime / WorkSource protocol | Excellent long-term reframe but it's a *different feature* (multi-tracker), not a scheduler |
| 6 | Per-turn observer / prompt-telemetry sidecar | Not scheduling — a different product for the prompt-analysis use case |
| 7 | Persistent-actor / "done-is-a-lie" workflow lifecycle | Orthogonal lifecycle change, not a scheduling mechanism |
| 8 | Artifact feedback-loop workflows (produces/consumes) | Interesting adjacent feature; deserves its own ideation |
| 9 | Workspace lifecycle modes / missed-fire policy / CLI verbs / dashboard panel / JSONL ledger / overlap policy / local tracking file | Real concerns but *implementation details* of whichever mechanism wins — absorbed into idea #2 rather than scored alone |
| 10 | 11 use-case ideas (autopsy, lint bot, librarian, A/B, canary, token governor, auto-tuner, curator, cross-refactor, rework distillation, ledger compaction) | Cut from mechanism filter — captured in "Capabilities Unlocked" section above |

---

## Observations Worth Surfacing

1. **#1 and #2 aren't different ideas — they're MVP and full-feature of the same idea.** The real decision is how many knobs to ship.
2. **#4 is the honest competitor to #1/#2.** For a hosted Fargate platform targeting 3–5 Enterprise teams, EventBridge + one HTTP endpoint may be the correct answer. The internal scheduler only wins if in-Stokowski observability matters more than minimum code.
3. **#3 is the most elegant *if* you're willing to refactor gates.** It eliminates the scheduler as a separate concept, but gate semantics today assume a Linear state to advance to, and recurring workflows violate terminal-state invariants.
4. **#5 and #6 are composition layers, not base mechanisms.** Both need a base scheduler (or HTTP trigger) to park work against. Treat as "also ship" decisions.
5. **None of the six ideas let you run `/compound-refresh` without *some* target workspace.** Scheduled jobs with accumulating state need persistent workspaces — any winning mechanism must answer the workspace question.

---

## Session Log

- 2026-04-19: Initial ideation — 40 raw candidates across 4 frames (operator pain, inversion, reframing, leverage), deduped to ~30 distinct ideas, 6 mechanism survivors. User selected idea #3 (gate-with-resolver generalization) for brainstorming.
