---
date: 2026-04-19
topic: scheduled-jobs
---

# Scheduled Jobs

## Problem Frame

Stokowski today dispatches work only in response to human-created Linear issues. There is no way to run recurring autonomous work: periodic repo hygiene (e.g. `/compound-refresh`), background operational analysis of workflow prompts, nightly audits of agent run logs, or anything else that should happen on a cadence rather than a ticket.

Operators currently have two bad options: run such work manually (forgotten, inconsistent) or spin up external schedulers (EventBridge, cron) that each tenant must configure separately and whose outputs live outside the Stokowski observability surface.

The goal is first-class support for scheduled/recurring autonomous work that:
- Integrates cleanly with existing Stokowski primitives (state machine, tracking, workspaces, gates, multi-workflow routing) without introducing a parallel runtime
- Makes Linear the operator's control plane for scheduled work, just as it is for ticket-driven work
- Gives each fire a first-class audit trail (gate-reviewable, PR-linkable, rework-compatible)
- Lets operators change cadence, pause, trigger-now, and kill jobs without a deploy

## Requirements

### Schedule Type and Instance Model

- **R1. Schedule types are declared in workflow.yaml.** A new top-level `schedules:` block defines schedule *types*. Each entry has an identifier (e.g. `compound-refresh`), a target workflow reference, and fire-time policies (overlap_policy, workspace_mode, on_missed, retention_days, max_runtime_ms). Schedule types are definition-state: they require code review and deploy to change.

- **R2. Schedule instances are Linear issues.** Operators create a Linear issue, label it with `schedule:<type>` (matching a type declared in workflow.yaml), and that issue becomes a **template**. Many templates can bind to the same schedule type. Templates are operator-owned: created, paused, renamed, archived, or moved to a terminal "Closed" Linear state without a deploy. See R21 for the explicit lifecycle contract (archive, terminal-state, and hard-delete behavior — they are not interchangeable).

- **R3. Cron expression lives on the template (operational state).** The cron expression is attached to the template issue — not to workflow.yaml — so operators can change cadence without a deploy. Precedence is **per-template**: the `Cron` Linear custom field is consulted first if it exists for the workspace; if the field is blank or unset on a specific template, description YAML front matter (`cron:` key) is used as a per-template fallback. The same per-template precedence applies to the `Timezone` field (R22). This avoids the silent-migration footgun where a workspace adopts custom fields but a specific template's YAML value gets ignored.

- **R4. Templates are not dispatched as agents.** Any issue carrying a `schedule:*` label is excluded from `_is_eligible()` checks — templates live in Linear as configuration rows, not as dispatch-eligible work. Their only active role is being read by the schedule evaluator each tick.

- **R5. Children represent fires.** When the evaluator decides a template is due, Stokowski creates a **child Linear issue** (using Linear's native sub-issue/parent relationship) representing that specific fire. The child carries the target workflow's routing label (e.g. `workflow:compound-refresh`) so the existing multi-workflow routing logic picks it up unchanged.

- **R6. Children flow through normal dispatch.** A child issue is a completely normal Stokowski work unit after creation: normal `_is_eligible` → normal `_dispatch` → normal workflow pipeline → normal terminal cleanup. Every existing primitive (tracking comments, gates, rework, PR linking, `RunAttempt`, token accounting, stall detection, retries) works on the child with zero modification.

### Fire Evaluation

- **R7. Schedule evaluation happens once per tick.** The orchestrator evaluates all templates at the top of `_tick()`, before reconciliation and dispatch. Evaluation is a pure function of (template set, now). Hot-reload of workflow.yaml each tick continues to work because schedule types are config.

- **R8. Overlap policy is per-schedule-type.** Declared in workflow.yaml. Supported values:
  - `skip` (default): if any child of this template is still in-flight, drop the fire.
  - `queue`: create the child anyway; per-state concurrency caps will serialize it.
  - `cancel_previous`: cancel in-flight children before creating the new child. "Cancel" is not a silent `_kill_worker` — the in-flight child is moved to a terminal "Canceled" Linear state, a tracking comment is posted explaining the cancel (including the triggering fire's slot), and any attached PR is marked as closed/abandoned. Children currently sitting at a gate (awaiting human review) are ALSO canceled — this avoids wasting reviewer time on work that will be discarded — and the cancel comment makes the reviewer visible-in-Linear.
  - `parallel`: always create the child; allow true concurrent runs.

- **R9. Missed-fire policy is per-schedule-type.** Declared in workflow.yaml. Supported values:
  - `skip` (default): on restart, next fire is the next scheduled time; missed fires are lost.
  - `run_once`: if any fires were missed during downtime, catch up with a single fire.
  - `run_all` (bounded): catch up with up to N missed fires (N is a capped constant, e.g. 5).

- **R10. Pause is a Linear state on the template.** Moving the template to a `Paused` Linear state suppresses fire evaluation. Moving it back to `Scheduled` resumes evaluation (subject to the `on_missed` policy for the pause duration).

- **R11. Trigger-now is a Linear state transition.** Moving the template to a designated "trigger" Linear state (e.g. `Trigger Now`) causes Stokowski to create a child immediately out-of-band, then move the template back to `Scheduled`. This provides a Linear-native "run this job right now" affordance without a separate CLI or API.

### History and Retention

- **R12. Fire history is the children list.** The complete audit trail for a scheduled job is the list of child issues under its template. Each child carries its own tracking comments, gate decisions, rework chain, PR links, and run logs. No cross-fire history mutation is possible.

- **R13. Completed children are retention-managed (best-effort).** Each schedule type declares a `retention_days` policy. Stokowski archives children via Linear's archive mutation N days after they reach a terminal state. The template is never archived by the retention sweep. Retention is best-effort: transient archive failures are retried with bounded backoff up to a cap; persistent failures (e.g. permission denied, mutation missing) are surfaced in operator-visible diagnostics (dashboard + logs) and the sweep continues with other children — a single poison-pill child never blocks the whole sweep.

- **R14. Run logs follow existing retention.** Child agent runs produce NDJSON logs under the existing `LoggingConfig.log_dir` path. The existing `max_age_days` / `max_total_size_mb` sweep governs them. No new logging primitive.

### Workspace Lifecycle

- **R15. Workspace mode is per-schedule-type.** Declared in workflow.yaml:
  - `ephemeral` (default): workspace is created per-child and removed at terminal, matching current behavior.
  - `persistent`: workspace is keyed by *template* identifier (not child), survives across fires, gets the `after_create` hook once on first fire and `before_remove` only on template deletion. Required for jobs that build up state across runs (e.g. `.context/` for compound-refresh).
  - `shared:<key>`: multiple schedule types share a workspace keyed by `<key>`, with an exclusion lock per fire. Useful when multiple analysis jobs study the same checkout.

- **R16. Workspace-mode transitions require explicit acknowledgement, not just a warning.** If an operator changes `workspace_mode` in workflow.yaml for a schedule type that has existing template instances, Stokowski refuses to start (or to hot-reload the change) until the operator provides an explicit acknowledgement — either a `--accept-workspace-reset` CLI flag or a marker file in the workflow dir (`.schedule-workspace-reset-ack`). The refusal message lists which templates will have orphaned workspaces so the operator can decide whether to clean them up. This is deliberately heavier than a warning: multi-change deploys bury warnings, and silent workspace orphans are expensive to recover from.

### Configuration and Validation

- **R17. Schedule-type config is validated at config load; cron is validated at evaluation time.** `validate_config()` verifies the deploy-time surface: every `schedules.<type>.workflow` references a defined workflow, `retention_days ≥ 1`, policy enums are valid, and required Linear states exist for `Scheduled` / `Paused` / `Trigger Now` if templates are in use. Cron expressions are *not* validated at config load because they live on Linear templates, not in workflow.yaml — they are validated by the schedule evaluator the first time it reads each template (see R18 for invalid-cron handling).

- **R18. Invalid cron is surfaced loudly on the template, not just in logs.** If a template carries an unparseable cron, the evaluator (a) logs the error, (b) posts a structured tracking comment on the template issue itself (`<!-- stokowski:schedule_error expr="..." reason="..." -->` plus human-readable text) — idempotent, one comment per distinct error, not once per tick, and (c) moves the template to a distinguishable `Error` Linear state. Templates in `Error` state are excluded from fire evaluation (they are among the reserved schedule states per R24, and the evaluator skips them in its fetch-or-filter pass). An operator fixing the cron and returning the template to `Scheduled` clears the error on the next evaluator pass. This replaces the passive "diagnostic surface" approach: silent schedule disable is exactly the original pain ("forgotten, inconsistent") the feature is meant to solve. If the comment-write itself fails (e.g. Linear permission denied), the evaluator falls back to a structured log line in `logging.log_dir` so some record exists even when Linear is unavailable. Aggregate "templates in Error for > 24h" is surfaced in the dashboard so a systemically ignored error state is not hidden behind per-template surfaces.

- **R19. Schedule types are hot-reloadable with workflows.** Changes to `schedules:` in workflow.yaml take effect on the next tick, consistent with existing workflow hot-reload behavior. When a schedule type is removed while template instances still carry its label, those templates enter the error state defined in R18 (`Error` Linear state + tracking comment) with reason "schedule_type_removed" — they do not silently stop firing.

### Reliability and Edge Cases

- **R20. Fire idempotency via template watermark comments.** Every fire is keyed by `(template_id, cron_slot)` where `cron_slot` is the canonical timestamp the cron expression resolved to (not wall-clock now). The watermark protocol has an explicit three-state machine per `(template_id, cron_slot)` pair:
  1. **No watermark** → evaluator posts `<!-- stokowski:fired template=<id> slot=<ISO8601> status=pending -->`, then attempts child creation, then updates the watermark to `status=child id=<child-id>` on success OR `status=failed reason=<code> attempt=<n>` on failure.
  2. **Watermark exists with `status=pending` or `status=failed attempt<MAX`** → evaluator re-attempts child creation with exponential backoff (mirrors existing `max_retry_backoff_ms`). After `MAX` attempts (planning-defined, suggested 5), watermark transitions to `status=failed_permanent reason=<code>`; the template moves to `Error` state per R18 with a distinct reason, breaking the retry loop.
  3. **Watermark has `status=child`** → slot is done, no-op.
  
  Assumed single-writer per tenant (see Dependencies). Watermarks posted by the orchestrator are Stokowski-authoritative; if an operator deletes a watermark comment in Linear, the slot is treated as unfired (accepted failure mode, documented for operators). The same comment convention already used for state/gate tracking in `tracking.py` is the pattern. The watermark update in step 1 may be implemented as either a `commentUpdate` mutation or as a follow-up comment that supersedes the prior one (oldest-first parsing per `tracking.py`) — choice deferred to planning based on Linear API verification.

- **R21. Template lifecycle contract (archive vs. terminal vs. hard-delete).**
  - **Archive (recommended)** — Moving a template to archived state stops future fires immediately; in-flight children complete normally. Archived templates are excluded from evaluator passes.
  - **Terminal "Closed" Linear state** — same behavior as archive for the evaluator (no future fires, in-flight children complete). Difference is cosmetic in Linear UI; both are supported.
  - **Hard-delete** — explicitly unsupported. Stokowski detects hard-deletion via reconciliation returning "issue not found" for a template with in-flight children or persistent workspace. Because hard-delete cleanup is destructive (cancels running children, removes persistent workspace and Docker volumes), detection requires **N consecutive reconciliation passes** (planning-defined threshold, suggested 3) returning "not found" before cascade cleanup triggers — single-tick transient errors, eventual-consistency lag, or conflated API errors do not destroy persistent state. The first detection logs a warning; the Nth triggers the cascade. Children canceled via the R8 `cancel_previous` protocol; persistent workspaces flagged for removal at next orchestrator cleanup pass; a loud warning is logged. The flagged-for-removal set is re-derived from filesystem scan on startup so crashes during a cascade cleanup recover on next orchestrator start.
  - **"Closing a template"** in all other requirements and success criteria means "archive or move to Closed state" — the two are interchangeable for evaluator behavior.

- **R22. Cron expressions have explicit timezone semantics.** Cron is evaluated in UTC by default. A template MAY override the timezone via a `Timezone` Linear custom field (or `tz:` key in the description YAML front matter fallback — same per-template precedence as R3) containing a **strict IANA timezone name** (e.g. `America/New_York`). The validator is strict — `"PST"`, `"Pacific Time"`, `"US/Pacific"`, `"GMT-8"`, and lowercase/whitespace-padded variants are rejected and trigger the R18 error surface with reason `"invalid_timezone"` including the received value and an example correct value ("expected IANA like `America/Los_Angeles`"). DST behavior for ambiguous slots (spring-forward 02:30 AM, fall-back repeated hour) is specified in the planning doc alongside the `croniter` version pin and `zoneinfo.ZoneInfo` wrapper; the chosen behavior MUST be documented in operator-facing docs so surprise is minimized. Multi-tenant Fargate deployments MUST default to UTC — reliance on server-local time is explicitly forbidden.

- **R23. Persistent workspace lifecycle.** For `workspace_mode: persistent`:
  - Workspace key is the **template** identifier, not the child. `ensure_workspace` / `remove_workspace` signatures are extended to accept an explicit key.
  - `after_create` hook runs **once** on first fire of the template; it does not run on subsequent fires even though the child identifier differs.
  - `before_remove` hook and workspace teardown run **only** on template lifecycle exit (archive, terminal state, detected hard-delete per R21) — NOT on child-terminal transitions. Child-terminal cleanup is a no-op for persistent workspaces.
  - Volume lifecycle (Docker mode): persistent workspaces use named Docker volumes that are NOT removed by `cleanup_orphaned_containers()`. A separate template-exit path removes volumes.

- **R24. Schedule evaluator uses a dedicated Linear fetch path.** Templates live in reserved Linear states (`Scheduled`, `Paused`, `Trigger Now`, `Error`) that are not in `active_linear_states()`. The evaluator therefore cannot reuse `fetch_candidate_issues()` — it needs a dedicated query: "issues with label matching `schedule:*` in any of the reserved schedule states." This query runs once per tick. The template-specific Linear states are declared under a new `linear_states.schedule:` sub-block in `LinearStatesConfig` so operators can map them to their tenant's Linear state names.

- **R25. Scope guardrail carve-out: children may post tracking comments to their own template.** Agents today are prohibited from writing to other Linear issues (see `SCOPE_RESTRICTION_SYSTEM` in `runner.py` and the lifecycle section). Children of a scheduled template are an explicit exception: they MAY post comments to the parent template (and only the parent template) for cross-fire status coordination (e.g. "last run wrote 3 new entries to `.context/`"). The carve-out is coded in the scope guardrail text, not enforced at the tool-permission layer — it is a probabilistic guardrail consistent with Stokowski's existing model.

- **R26. Duplicate template labels are a validation error, not a feature.** Two templates carrying the **same** `schedule:<type>` label MUST be flagged by the evaluator as a configuration error. The duplicates are ordered by `(createdAt ASC, identifier ASC)` — the earliest-by-creation-timestamp template wins; ties (identical `createdAt`) are broken lexicographically by Linear issue identifier so the winner is deterministic across ticks. All losers are moved to the R18 `Error` state with reason `"duplicate_schedule_label"`. Per-template divergent policies are not supported (policies live on the schedule type, not the template); operators wanting divergent policies should declare distinct schedule types. Note: R2 permits multiple templates to exist for the same schedule type, but each must carry a **unique** label (e.g. `schedule:compound-refresh` and `schedule:compound-refresh-staging` as two distinct types in workflow.yaml), not two templates with identical labels.

## Success Criteria

- Operator can define a new scheduled job end-to-end by: adding a `schedules:` entry to workflow.yaml, deploying, creating a Linear template issue with the matching label, setting a cron. First fire happens within the declared cron window without any further action.
- Changing cron on an existing job is a Linear edit only (no deploy).
- Pausing a job is a Linear state change only; unpausing resumes normally.
- `/compound-refresh` running weekly against a persistent workspace demonstrably preserves `.context/` state between fires.
- A fire that triggers a gate (e.g. human PR review on the child) blocks that child alone. Subsequent fires on the same schedule proceed according to the declared overlap policy.
- Completed child issues archive automatically after `retention_days` without operator intervention.
- Existing single-workflow and multi-workflow configs without a `schedules:` block continue to work unchanged.
- Archiving a template (or moving to a terminal Closed state) stops future fires; in-flight children complete normally. Hard-deletion triggers a loud cascade cleanup per R21.
- A template with an unparseable cron is discoverable in Linear within one tick (error state + visible comment on the template), not silently disabled.
- A crash between fire-watermark comment and child-creation recovers on next tick without duplicate or lost fires (R20 idempotency).
- Cron expressions specified without an explicit timezone fire in UTC; expressions on a template with a `Timezone` field fire in that zone across DST transitions.

## Scope Boundaries

- **Not in scope: non-Linear scheduled jobs.** Scheduled work always manifests as a Linear template + children. If operators want cron-driven work with no Linear footprint, they can use EventBridge/k8s/cron hitting an HTTP trigger (idea #4 from the ideation) — this is a separate future feature.
- **Not in scope: unified resolver abstraction.** The broader "gate-with-resolver" generalization (clock / artifact_changed / webhook / workflow_completed) is explicitly NOT built here. Only the clock dimension is shipped, as scheduling-through-templates. Other resolvers may be added later if justified by their own use cases.
- **Not in scope: agent-drafted schedules.** Agents cannot emit `<!-- schedule:next at=... -->` directives in this iteration. Schedules are operator-declared.
- **Not in scope: invariant-based scheduling.** The "declare desired state, fire on violation" approach (idea #6) is not built. All fires are cron-driven.
- **Not in scope: per-fire parameter overrides.** Children are created from templates with no fire-time parameter injection. If operators need parameterized runs, they create separate templates.
- **Not in scope: dynamic schedule generation.** Templates are human-created in Linear. The self-observing proposer idea (system suggests schedules from usage) is deferred.
- **Not in scope: workflow-completion or cross-template dependencies.** Templates fire independently on their own crons; there is no "run B after A" wiring.

## Key Decisions

- **workflow.yaml defines types; Linear templates are instances.** Matches the existing "types-in-code / instances-in-Linear" pattern Stokowski already uses for workflows. No new mental model.
- **Cron lives on the template (operational state), not workflow.yaml (definition state).** Separates change-control surfaces: workflow binding needs code review; cadence can be operator-edited. Security-relevant: someone with Linear access alone cannot repoint a template at a privileged workflow.
- **Parent/child over single cycling issue.** Rejects the immortal-issue-that-cycles-states model (Option A from the brainstorm). Run-as-child makes each fire first-class, composable with gates/rework/PR review/tracking, and avoids comment explosion on long-lived jobs.
- **Children inherit existing routing.** Children carry a `workflow:<name>` label; the existing multi-workflow routing (`R2` in multi-workflow-requirements.md) picks them up unchanged. No new dispatch path.
- **Clock resolver only.** No broader resolver abstraction. Build the specific feature with its actual use cases; don't speculate on `webhook` / `artifact_changed` / `workflow_completed` resolvers until they have their own concrete use cases.
- **Trigger-now is a Linear state, not a CLI verb or API endpoint.** Reuses the existing Linear-as-control-plane discipline. A dashboard button or CLI can be added later without changing the core mechanism.
- **Template is config, not a dispatchable work unit.** The `schedule:*` label is a hard exclusion in `_is_eligible()`. Templates never enter the dispatch pipeline themselves — they are read-only to the orchestrator.
- **Idempotency via template watermark comments, not an external store.** R20 uses the existing structured-comment convention (`<!-- stokowski:fired ... -->`) rather than introducing a new durable store. Matches how state/gate tracking already survives crashes.
- **Hard-deletion is unsupported, not unhandled.** R21 makes archive/terminal-state the documented paths and adds cascade cleanup for hard-delete as a recovery mechanism, not a first-class path.
- **Invalid cron fails loudly on the template.** R18's error state + visible comment was chosen over passive diagnostics because silent schedule disable is exactly the pain this feature is meant to solve.
- **Timezone defaults to UTC.** R22 forbids reliance on server-local time for multi-tenant Fargate. Per-template override via Linear field is the escape hatch.
- **Single writer per tenant.** The scheduler's idempotency (R20), hard-delete detection (R21), and error-state management (R18) all assume a single orchestrator replica writes to a given tenant's Linear workspace. The hosted Fargate MVP enforces this at the deployment layer (one task per tenant) — see Dependencies. Lifting this constraint in the future would require a compare-and-swap primitive (DynamoDB conditional write, Redis SETNX, or equivalent) since Linear comment creation has no CAS semantics. Deliberately not building that infrastructure now.

## Dependencies / Assumptions

The first three are load-bearing unverified assumptions — see "Resolve Before Planning" in Outstanding Questions. Not moved here as accepted assumptions because the design doesn't survive any of them being false.

- **Load-bearing:** Linear sub-issue API, custom-field read/write, and archive mutation. (See Outstanding Questions → Resolve Before Planning.)
- The multi-workflow implementation (see `docs/brainstorms/2026-03-24-multi-workflow-requirements.md`) is already in code paths for label-based workflow selection. Scheduled-job children rely on that routing. (Verified in `_resolve_gate_workflow` and related.)
- Structured Linear comments (`<!-- stokowski:state ... -->` convention) are a stable, reliable primitive for both state tracking and fire watermarks (R20). (Verified — `tracking.py` uses them today.)
- A cron parsing library (`croniter` or equivalent with timezone-aware evaluation) will be adopted as a new dependency. (Confirmed absent today.)
- Hosted Fargate MVP context: this design targets 3-5 internal Enterprise teams. Per-tenant scale implies <100 templates per tenant; fire rates measured in fires-per-hour, not per-second. Multi-tenant deployment mandates UTC-default cron per R22.
- **Single-replica-per-tenant deployment.** The design explicitly assumes exactly one orchestrator process writes to a given Linear workspace at a time. Rolling deploys (task replacement) must use serial replacement, not blue/green overlap — brief windows of zero orchestrators are acceptable (fires queue via R20 watermarks and resume on the next tick); windows of two concurrent orchestrators are NOT acceptable (would cause duplicate fires, duplicate watermark comments, and conflicting state transitions). Fargate task definition should use `maximumPercent: 100`, `minimumHealthyPercent: 0` for deploys to enforce serial replacement. See Key Decisions → Single writer per tenant.

## Visual Aid

```
        workflow.yaml (types)               Linear (instances)
        ─────────────────────               ───────────────────
          schedules:                         ┌─ Template issue
            compound-refresh:                │   Title: "compound-refresh weekly"
              workflow: refresh-wf           │   Labels: [schedule:compound-refresh]
              overlap: skip         ◄────────┤   Cron: "0 8 * * 1"
              workspace: persistent          │   State: Scheduled | Paused | Trigger-Now
              retention_days: 30             │
                                             │   └─ Child issues (sub-issues)
                                             │       SMI-199 (2026-04-12 fire) — Done
                                             │       SMI-200 (2026-04-19 fire) — Done
                                             │       SMI-201 (2026-04-26 fire) — Running
                                             │
                                             │   Children carry label
                                             │   workflow:refresh-wf, flow through
                                             │   normal multi-workflow dispatch
                                             │
          workflows:                         │
            refresh-wf:                      │
              path: [refresh, done]  ◄───────┘ (children bind here via label)
```

**Fire sequence per tick:**

```
 _tick()
   │
   ├─► fetch_templates()                              [dedicated Linear query — R24]
   │     schedule:* labels × reserved schedule states
   │
   ├─► evaluate_schedules(templates, now, watermarks)
   │     ├─ parse cron + timezone (UTC default — R22)
   │     ├─ skip template states: Paused, Error, archived
   │     ├─ handle Trigger-Now (fire once, reset to Scheduled)
   │     ├─ apply overlap_policy against in-flight children (R8)
   │     ├─ apply on_missed for startup catch-up (R9)
   │     ├─ validate cron; route invalid → R18 error surface
   │     ├─ detect duplicate labels → R26 error
   │     └─ return list of (template, cron_slot) to materialize
   │
   ├─► for each (template, cron_slot):               [Fire sequence — R20]
   │     1. post watermark comment: `<!-- stokowski:fired slot=... -->`
   │     2. create_child(template, workflow_label)  [Linear sub-issue API]
   │     3. update watermark comment with child id
   │
   ├─► reconcile()        (existing) + template hard-delete detection (R21)
   │
   ├─► fetch + dispatch   (existing; picks up children normally — R6)
   │
   └─► retention_sweep    (archive terminal children older than retention_days — R13)
```

## Outstanding Questions

### Resolve Before Planning

These are load-bearing — the design assumes specific Linear API capabilities that `linear.py` does not currently exercise. At least R3, R5, R12, R13, R20, and R21 depend on them. Verify existence and shape before planning proceeds; if any capability is missing, the corresponding requirement needs a different mechanism.

- [Affects R3][Needs research] **Linear custom field read/write GraphQL shape.** Confirm the API call to read a named custom field value on an issue, whether it is available on the plan tiers the target tenants use, and whether writes (e.g. updating a `Timezone` field from Stokowski) are possible. If custom fields are unavailable, R3 and R22 fall back to description YAML front matter — verify that fallback path is complete.
- [Affects R5, R12, R13, R21][Needs research] **Linear sub-issue and archive API.** Verify: (a) how to create a sub-issue via `issueCreate` with a parent reference, (b) how to query children of a parent with pagination, (c) whether `issueArchive` is a mutation, a state transition, or both, and (d) whether archiving a parent cascades to children (affects R21 hard-delete cascade behavior).
- [Affects R20][Technical] **Tracking-comment idempotency primitive.** Confirm that Linear comments support structured hidden-HTML content (the existing `<!-- stokowski:state {...} -->` convention), are readable via `COMMENTS_QUERY`, and that comment ordering is reliable enough to serve as a watermark. If not, R20 needs a different store (e.g. local JSONL next to `logging.log_dir`).

### Deferred to Planning

- [Affects R10, R11, R18, R24][Technical] Canonical Linear state names for `Scheduled`, `Paused`, `Trigger Now`, `Error`. Extend `LinearStatesConfig` with a `schedule_states:` sub-block; planning defines the exact shape and defaults.
- [Affects R7, R8, R20, R24][Technical] Schedule evaluator placement. New module `stokowski/scheduler.py` exposing a pure `evaluate_templates(templates, now, watermarks) -> list[FireDecision]` is preferred; confirm this composes with `_tick()` phase ordering (evaluator runs before reconcile/dispatch).
- [Affects R8][Technical] `cancel_previous` in-flight-children enumeration: per-template running-children index (`template_id -> set[child_id]`) plus reverse index (`child_id -> template_id`), both symmetric with `_cleanup_issue_state()` per the documented footgun.
- [Affects R6][Technical] `build_lifecycle_section` additions for children: small "this is a fire of template X on schedule Y, slot Z" block plus R25 scope carve-out text.
- [Affects R15, R23][Technical] For `shared:<key>` workspace mode, per-key asyncio lock is the concurrency primitive; planning confirms composition with existing per-issue concurrency bookkeeping and handles lock-holder crash recovery.
- [Affects R1, R15, R23][Needs research] Docker/DooD plugin-shim adaptation for `persistent` workspaces living across container restarts — the existing bind-mount model may need a different lifecycle.
- [Affects R22][Needs research] `croniter` (or alternative) DST-transition behavior, leap-second handling, and whether it supports timezone-aware evaluation directly or needs a `pytz`/`zoneinfo` wrapper.
- [Affects R21][Technical] Template hard-delete detection path: extend reconciliation to flag templates whose `issues(filter:{id:{in:$ids}})` response is empty, and route to the cascade cleanup. Confirm this does not conflict with existing "gated issue not found" cleanup in `_reconcile()`.
- [Affects R11, D4 latency][Technical] Whether Trigger-Now fires should force-schedule an immediate follow-up `_tick()` to pick up the just-created child, reducing worst-case latency from ~60–90s to ~1–5s.
- [Affects R8][Technical] Ordering and partial-failure handling across the three mutations `cancel_previous` performs (child state transition → tracking comment → PR close). If any mutation fails: retry policy, rollback behavior, and the "child raced to terminal during cancel" and "child at mid-gate while human is typing" edge cases.
- [Affects R16, R19][Technical] Hot-reload refusal mechanism when workspace_mode changes on a live schedule type with existing templates. Questions: what orchestrator state applies during refusal (continue with old config, stop dispatching, enter degraded mode); how the `.schedule-workspace-reset-ack` marker file is consumed (single-use with timestamp/hash match, or persistent); whether in-flight children of the reshaping schedule complete under old or new semantics.
- [Affects R25][Technical / needs research] Detection of cross-template writes by scheduled-job agents. The carve-out is probabilistic and permits writes to the agent's own template only. Options: no detection (accept blast radius at 3-5 internal teams), periodic audit (Stokowski enumerates comments by its own API key and checks commenter-child parent_id matches the commented-on template), or tool-permission-layer enforcement (requires Linear MCP tool-scope support). Multi-tenant scale may demand option 2 or 3.
- [Affects R23][Technical] No-op semantics for `remove_workspace(child_id)` on persistent-mode templates. Either callers must be aware of workspace_mode and skip the call, or the function gains a "child of persistent template" flag and no-ops internally. Affects 7 call sites in `orchestrator.py` and `workspace.py`.
- [Affects R20][Technical] Watermark "update" implementation: `commentUpdate` mutation (if Linear supports it — see Resolve Before Planning) vs. a sequence of supplementing comments with oldest-first parsing. Decision blocked on Linear API verification.

## Next Steps

→ `/ce-plan` for structured implementation planning
