---
title: "Watermark-based idempotency protocol for scheduled sub-issue materialization"
module: scheduled-jobs
date: 2026-04-19
problem_type: best_practice
component: background_job
severity: high
applies_when:
  - "Materializing durable child records from a scheduled template backed by an external store (e.g. Linear)"
  - "Crash/restart recovery must not double-fire or skip a cron slot"
  - "Multiple watermarks can land within the same millisecond (parallel overlap or Trigger-Now racing a cron slot)"
  - "Single-writer-per-tenant deployment constraint is enforced (ECS desiredCount:1)"
related_components:
  - orchestrator
tags:
  - watermark
  - idempotency
  - scheduled-jobs
  - crash-recovery
  - cron
  - seq-tiebreak
  - single-writer
  - linear
---

# Watermark-based idempotency protocol for scheduled sub-issue materialization

## Context

Stokowski polls Linear on a configurable interval to find new work. When a scheduled template's cron slot comes due, the orchestrator must materialize a child sub-issue in Linear to represent that fire. The problem: the same poll tick can fire multiple times (e.g., after a process restart), the orchestrator can crash at any point during a three-step mutation sequence (post comment, create issue, post result comment), and Linear is the only durable store — there is no local database.

Without a disciplined protocol, the naive approach fails in multiple ways: a crash between creating the child and recording that creation causes a second child on the next tick; a retry after a transient failure creates a duplicate if the first attempt partially succeeded; two orchestrator processes racing the same template both materialize children for the same slot. An ad-hoc "fire and hope" approach produces ghost children — real Linear issues that the agent will work on, duplicating work and corrupting state-machine expectations. The watermark protocol addresses this by making every intermediate step detectable and recoverable, with the external store (Linear comments) as the ledger.

**Alternatives that were considered and rejected** (session history):
- **Immortal cycling issue** — a single Linear issue that cycles through `active → sleeping → active`. Rejected because "run" is not a first-class concept under this shape: gates, tracking comments, rework, PR links, and `RunAttempt` all pin to an issue; a fire that enters a gate blocks the issue from cycling; and 52+ weekly tracking comments on one thread becomes unusable.
- **Gate-with-resolver generalization** — extending the existing gate abstraction with `resolver: clock | webhook | …`. Rejected because `validate_config` requires at least one terminal state (a recurring `refresh → sleep → refresh` loop has none), and the unification only pays off if 2-3 resolvers ship — MVP scope made speculative framework-building out of scope.
- **Synthetic-issue ledger** — tracking fires in a local JSONL file with synthetic `Issue` objects. Rejected because it splits state across two stores (Linear comments + local file), leaves no Linear footprint for human review (gate decisions, PR links, rework), and doesn't compose with existing gate flow.
- **External CAS primitive** (DynamoDB / Redis SETNX) for multi-writer safety. Rejected as deliberate scope discipline: the hosted Fargate MVP pins one writer per tenant at the infra layer, so watermarks don't need CAS semantics.

## Guidance

### The 5-step materialization protocol (applies per template, per slot)

**Step 1 — Duplicate-sibling check FIRST.** Before any write, query Linear for all non-archived children of the template carrying the label `slot:<canonical>`. If any child exists (in any state — active, completed, canceled, but not archived), the slot was already materialized. Post `status=child` with the found child's identifier and return. This is the recovery path for crash-between-create-and-watermark-update. Critically, this check must include terminal children (completed/canceled): a child that ran to completion is still proof the slot fired. Filtering out terminal children (the pre-fix behavior caught in code review finding REL-001 / P1-03) breaks idempotency — on the crash-recovery path the terminal child is the only evidence the slot exists.

**Step 2 — Pre-create watermark.** Post `<!-- stokowski:fired {"template": "TPL-1", "slot": "2026-04-19T08:00:00Z", "status": "pending", "attempt": 1, "seq": 7} -->` to the template issue. This records intent before the potentially-lost mutation that follows. If the orchestrator crashes after step 2, the next tick can observe the pending watermark and know this slot is being worked on.

**Step 3 — Create child sub-issue.** Issue the `issueCreate` mutation in Linear with the slot label (`slot:2026-04-19T08:00:00Z`) attached. The slot label is the duplicate-detection primitive. If label resolution fails (the label does not exist in the Linear team), do NOT create the child without it — a label-less child is invisible to the step 1 dedup check and every subsequent tick materializes another one. Fail the attempt and increment the counter instead, giving the operator time to create the label.

**Step 4 — Post terminal watermark.** On success: post `status=child` with the new child's identifier and clear the fire-attempt counter. On failure: post `status=failed` with the attempt number and increment `_template_fire_attempts[(template_id, slot)]`.

**Step 5 — Retry budget.** After `MAX_FIRE_ATTEMPTS = 5` transient failures, post `status=failed_permanent` and move the template to the Error Linear state. This caps infinite retry on permanently broken configurations (missing team, broken Linear credentials, missing slot label).

### Cross-cutting requirements

**`seq` tiebreak.** Watermarks posted within a single millisecond must be ordered by a monotonic per-template counter, not by timestamp alone. The orchestrator owns this counter (`_template_watermark_seq[template_id]`), seeds it lazily on first sight of a template from `max_seq_from_parsed(parse_fired_by_slot(comments))`, and increments it via `_next_seq(template_id)` before each watermark write. Any parser that drops the `seq` field will mis-order co-millisecond fires.

**Single-writer-per-tenant.** Two orchestrators racing the same template can both pass the step 1 duplicate-sibling check (no child exists yet), both post pending watermarks, and both create children. Watermarks do not resolve this race — deployment must pin one writer per tenant (Fargate `desiredCount: 1`, `minimumHealthyPercent: 0`).

**Slot canonicalization.** Cron slots are ISO-8601 UTC at second precision (`2026-04-19T08:00:00Z`). Trigger-Now slots are `trigger:2026-04-19T08:00:00Z`. Namespaces are distinct; they can't collide. `canonicalize_slot()` in `scheduler.py` is the single source of truth — sub-second precision is dropped so the slot key round-trips through both watermark comments and the `slot:<ISO>` child label identically.

**Fire-attempt counter rehydration.** After restart, `_template_fire_attempts[(template_id, slot)]` must be seeded from the maximum `attempt` field in existing `failed` or `pending` watermarks for that slot. If the counter resets to 0 on every restart, a permanently-failing slot escapes the `MAX_FIRE_ATTEMPTS` cap — each new process lifetime grants five fresh attempts (code review finding REL-002 / P1-05). The seeding logic in `_materialize_fire` runs on first sight of a `(template_id, slot)` key not yet in `_template_fire_attempts`.

### Pseudocode sketch of `_materialize_fire`

```python
async def _materialize_fire(template, decision):
    slot = decision.slot
    key = (template.id, slot)

    # Rehydrate attempt counter on first sight after restart
    if key not in self._template_fire_attempts:
        seed_from_linear_comments(template.id, slot)

    attempts_before = self._template_fire_attempts.get(key, 0)

    # Step 1: duplicate-sibling check (all non-archived children)
    siblings = await fetch_all_non_archived_children(template.id)
    existing = find_existing_child_for_slot(siblings, slot)
    if existing:
        post_watermark(status="child", child_id=existing.id)
        return  # idempotent exit

    # Retry budget fast-path
    if attempts_before >= MAX_FIRE_ATTEMPTS:
        post_watermark(status="failed_permanent")
        move_template_to_error()
        return

    # Step 2: pre-create watermark
    post_watermark(status="pending", attempt=attempts_before + 1)

    # Resolve slot label — abort without creating labelless child
    resolved = await resolve_label_ids([slot_label_name(slot)])
    if slot_label_name(slot) not in resolved:
        self._template_fire_attempts[key] = attempts_before + 1
        return

    # Step 3: create child
    try:
        child = await create_child_issue(parent=template, labels=resolved)
    except Exception as e:
        self._template_fire_attempts[key] = attempts_before + 1
        post_watermark(status="failed", attempt=attempts_before + 1, reason=e)
        return

    # Step 4: terminal watermark on success
    post_watermark(status="child", child_id=child.id)
    self._template_fire_attempts.pop(key, None)
```

## Why This Matters

The protocol's value is in its crash-recovery proofs at each intermediate failure point.

**Crash between step 1 and step 2 (no writes at all).** The template has no pending watermark and no new child. The evaluator re-proposes the slot on the next tick as if nothing happened. Step 1 finds no sibling, protocol re-enters identically. Fully idempotent.

**Crash between step 2 and step 3 (pending watermark exists, no child).** The pending watermark's `_slot_has_terminal_watermark()` returns False (pending is explicitly non-terminal in `scheduler.py`). The evaluator re-proposes the slot. Step 1 finds no sibling. Step 2 sees the prior `pending` watermark in Linear — but the important thing is the attempt counter: because the counter was seeded from the pending watermark's `attempt` field, `attempts_before` reflects real history rather than resetting to 0. Protocol continues at step 3 with the correct attempt count.

**Crash between step 3 and step 4 (child exists with slot label, no terminal watermark).** This is the most important failure point — and why step 1 must come first. On restart, step 1 queries Linear children, finds the child carrying `slot:2026-04-19T08:00:00Z`, promotes the watermark to `status=child`, and returns. No duplicate child is created. The duplicate-sibling check at step 1 is what makes the entire step-3-through-step-4 crash window safe. If step 1 came after step 2, a pending watermark would already exist and the protocol would skip to step 3, creating a second child.

**Crash during `_cancel_child_for_overlap` (overlap policy = `cancel_previous`, 3-mutation protocol).** The cancel path runs a sequence of three mutations: transition sibling to Canceled state, post audit comment on child, post reference comment on template. If mutation 1 fails, `_cleanup_issue_state` must NOT be called on the sibling — the sibling remains in `self.running` so `_reconcile` continues monitoring it. Cleaning up local tracking before the Linear state transition is confirmed produces an orphan: Linear sees the child as still active, Stokowski has forgotten it, and `fetch_candidate_issues` re-dispatches it as a fresh issue on the next tick (code review finding REL-004).

**Template hard-delete detection.** `_reconcile` uses a 3-tick threshold (`TEMPLATE_HARD_DELETE_THRESHOLD_TICKS = 3`) before triggering `_cascade_template_delete`. The check set at `_reconcile` must include both `self._templates` and `set(self._template_children.keys())` — otherwise a template that was removed from `_templates` mid-lifecycle (e.g., after a config reload) but still has tracked children would never increment the absence counter and its children would become permanent orphans (code review finding P1-01).

## When to Apply

- Scheduled work is materialized as durable records in an external store that has no dedicated scheduling primitive (Linear issues as sub-issues, GitHub issues, Jira tickets).
- The external store provides an append-only comment or event stream that can function as an ordered ledger with structured payloads.
- The orchestrator process restarts are expected (container restarts, deployments, OS-level kills) and full recoverability without data loss is a hard requirement.
- Single-writer deployment per logical scheduling unit is achievable through infrastructure constraints (Fargate task pinning, Kubernetes deployment with `replicas: 1`).
- Transient upstream failures (Linear rate limits, network blips, GraphQL errors) are expected and the system must retry up to a bounded cap without operator intervention.
- Cron-style slot semantics are required: each firing must be uniquely and deterministically identifiable by a canonical key so the dedup primitive (the slot label) is stable across process restarts, clock drift within a second, and multiple in-progress fires.

## Examples

### Example 1 — Normal fire, no prior state

Template TPL-1 has cron `0 8 * * *` (daily at 08:00 UTC). Current time is `2026-04-19T08:00:05Z`. The evaluator computes `missed = [2026-04-19T08:00:00Z]` and emits `FireDecision(slot="2026-04-19T08:00:00Z", action="fire")`.

`_materialize_fire` begins:

1. `find_existing_child_for_slot(siblings, "2026-04-19T08:00:00Z")` — no siblings, returns None.
2. `attempts_before = 0`. Not at retry cap.
3. Posts pending watermark on TPL-1:
   `<!-- stokowski:fired {"template": "TPL-1", "slot": "2026-04-19T08:00:00Z", "status": "pending", "attempt": 1, "seq": 1} -->`
4. Resolves `slot:2026-04-19T08:00:00Z` label in Linear. Found. Proceeds.
5. `create_child_issue` succeeds. Returns SMI-42 with the slot label attached.
6. Posts success watermark on TPL-1:
   `<!-- stokowski:fired {"template": "TPL-1", "slot": "2026-04-19T08:00:00Z", "status": "child", "child": "SMI-42", "seq": 2} -->`
7. Clears `_template_fire_attempts[("tpl-1-id", "2026-04-19T08:00:00Z")]`.

TPL-1 now has two watermark comments. `parse_fired_by_slot` returns `{"2026-04-19T08:00:00Z": {"status": "child", "child": "SMI-42", ...}}`. On the next tick, `_slot_has_terminal_watermark` returns True for this slot — the evaluator never re-proposes it.

### Example 2 — Restart after crash between step 3 and step 4

Prior state: the orchestrator posted the pending watermark at `seq=1`, successfully called `create_child_issue` and received SMI-42 (with slot label `slot:2026-04-19T08:00:00Z` attached), then crashed before posting the success watermark.

TPL-1's comment timeline contains exactly one watermark:
`<!-- stokowski:fired {"template": "TPL-1", "slot": "2026-04-19T08:00:00Z", "status": "pending", "attempt": 1, "seq": 1} -->`

Process restarts. Next tick: `_slot_has_terminal_watermark` for the pending watermark returns False. Evaluator re-proposes the slot as `action="fire"`. `_materialize_fire` begins:

1. Fetches all non-archived children of TPL-1. Finds SMI-42 carrying label `slot:2026-04-19T08:00:00Z`. `find_existing_child_for_slot` returns SMI-42.
2. Step 1 fires the recovery path: posts terminal watermark on TPL-1:
   `<!-- stokowski:fired {"template": "TPL-1", "slot": "2026-04-19T08:00:00Z", "status": "child", "child": "SMI-42", "seq": 2} -->`
3. Returns immediately. No second child created.

The slot is now closed. On all subsequent ticks `_slot_has_terminal_watermark` returns True. SMI-42 is the unique materialization of this slot, exactly as if no crash had occurred.

## Common Pitfalls

- **Ordering step 1 after step 2 breaks crash recovery at the step-3-through-step-4 boundary.** If the pending watermark is posted before checking for an existing sibling, a crash between `create_child_issue` and the success watermark produces a state where step 1 on the next tick still sees no sibling — but a pending watermark exists. Without the sibling check being first, the protocol will call `create_child_issue` again, producing a duplicate. The rule is absolute: the sibling check must be the first action in the materialization function, before any write.

- **Losing the `seq` field in any watermark read or write path mis-orders co-millisecond fires.** The sort key in `_fired_sort_key` is `(timestamp, seq, input_index)`. If `seq` is omitted from a write, `_parse_fired_payload` returns a payload with no `seq` key, which `_fired_sort_key` maps to `seq_val = -1`. A new watermark written one millisecond later with `seq=0` will sort before an older watermark with `seq=5` only if the timestamp comparison dominates. For the `parallel` overlap policy and Trigger-Now fires that race cron slots, this ordering is the correctness primitive. Always pass the orchestrator-owned seq counter to `make_fired_comment`.

- **Replacing the in-memory template set before reconcile observes the transition causes the hard-delete counter to never increment.** `_reconcile`'s `ids_to_check` is `self._templates | set(self._template_children.keys())`. If `_fetch_templates` atomically replaces `self._templates` with a fresh snapshot that omits a template that was just deleted from Linear, and `_template_children[template_id]` was already cleared by a prior cleanup, the deleted template ID no longer appears in `ids_to_check`. `_reconcile` never sees it as absent and `_template_last_seen` never increments. The fix: include `set(self._template_children.keys())` unconditionally in the check set (the P1-01 fix), so templates with in-flight children survive the transition even after being removed from `_templates` itself.

- **In-memory fire-attempt counter that resets on restart lets permanently failing slots escape the retry cap.** `_template_fire_attempts` is ephemeral. If a slot fails three times, the orchestrator restarts, and fails twice more across two restarts, the slot has now exceeded `MAX_FIRE_ATTEMPTS = 5` total attempts with no `failed_permanent` watermark ever posted — each restart reset the counter to 0. The fix: on first sight of a `(template_id, slot)` key, read existing watermarks from Linear and seed the counter from `max(attempt)` across any `failed` or `pending` entries. This seeding happens lazily in `_materialize_fire` gated by `if key not in self._template_fire_attempts`.

- **`_cleanup_template_state` symmetry footgun** (session history, extends an existing CLAUDE.md pitfall). The same discipline that governs `_cleanup_issue_state` — every per-issue tracking dict added to `__init__` must be mirrored in cleanup — applies to templates. Any new `_template_*` dict added to `__init__` must also be added to `_cleanup_template_state`; the hard-delete cascade depends on complete cleanup. Plan review caught this twice: once when the initial `_cleanup_template_state` pseudocode listed only 3 of 9 template-keyed dicts, and again when code review surfaced `_retention_poison_pill_counts` / `_retention_last_archive_at` as unbounded leaks because the cleanup path didn't cover them.

## References

### Same-session artifacts

- `docs/brainstorms/2026-04-19-scheduled-jobs-requirements.md` — R20 is the load-bearing requirement defining the three-state watermark machine (`pending → child / failed_permanent`), the `(template_id, cron_slot)` key, and the crash-recovery invariant.
- `docs/ideation/2026-04-19-scheduled-jobs-ideation.md` — records the mechanism survivors and full rejection log (immortal cycling, gate-resolver generalization, synthetic-issue ledger, JSONL-ledger).
- `docs/solutions/2026-04-linear-api-scheduled-jobs.md` — confirms the Linear API capabilities the protocol depends on: append-only `commentCreate`, sub-issue creation via `parentId`, `slot:<iso>` label resolution via `resolve_label_ids`, archive mutation semantics.
- `.context/compound-engineering/ce-code-review/20260419-220950-07ca8b73/` — review findings that closed protocol gaps: P1-01 (hard-delete cascade ids_to_check), P1-02 (Trigger-Now template reset), P1-03 (terminal-sibling inclusion), P1-04 (missing slot label), P1-05 (attempt counter rehydration), P1-07 (cancel_previous cleanup ordering). The `deferred.md` in the same directory tracks P1-11 (watermark author verification) as an open design question for the single-writer assumption.

### In-repo pitfalls

- `CLAUDE.md` "Scheduled jobs" section and "Common pitfalls" entries for Watermark `seq` field, `_cleanup_template_state` symmetry, single-writer-per-tenant deployment constraint, and workspace-mode change marker — all directly load-bearing for this protocol.

### Prior thinking

- GitHub issue [Sugar-Coffee/stokowski#12 — feat: persistent state for crash recovery](https://github.com/Sugar-Coffee/stokowski/issues/12) (closed) — early framing of the crash-recovery requirement that this protocol satisfies.
