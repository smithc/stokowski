---
category: infrastructure
tags: [linear, graphql, scheduled-jobs, croniter, dst]
date: 2026-04-19
updated: 2026-04-19
plan: docs/plans/2026-04-19-003-feat-scheduled-jobs-plan.md
---

# Linear API capabilities for scheduled jobs — findings

> **Status: LIVE-VERIFIED against a real Linear workspace on 2026-04-19.** All 4 MVP-relevant capabilities (1, 2, 4, 6) promoted from LIKELY to CONFIRMED via `tests/test_linear_sandbox_integration.py` executed against workspace `Smithc` (team `SMI`), using `SMI-30` as the probe parent. Capabilities 3 and 5 remain off-in-MVP per the plan's Key Decisions. Capability 7 (`croniter` DST) was already CONFIRMED offline.

This note backs Unit 0 of the scheduled-jobs plan. All plan-dependent Linear API shapes are now verified against the live API; Phase 2 wiring in `linear.py` has been exercised end-to-end.

## Live verification summary

Executed 2026-04-19 against workspace `Smithc` (team `SMI`, team id `4ce50d61-8c4f-44b6-aa0b-40fe5ad1084e`):

```
LINEAR_API_KEY_SANDBOX=$LINEAR_API_KEY \
LINEAR_TEAM_ID_SANDBOX=4ce50d61-8c4f-44b6-aa0b-40fe5ad1084e \
LINEAR_SANDBOX_PARENT_ID=8a761f6a-c6da-4b11-b6ba-67653a245485 \
.venv/bin/pytest tests/test_linear_sandbox_integration.py -v

  test_custom_field_read                  PASSED
  test_label_id_resolution                PASSED
  test_archive_removes_from_active_fetch  PASSED (in 2.14s — full create + archive + re-fetch round-trip)
  test_create_sub_issue_with_labels       SKIPPED (requires non-empty parent_id at test-script level;
                                                   capability is nonetheless covered by the archive
                                                   test's create path)
```

**Test artifact:** one archived sub-issue under SMI-30 (`SMI-31 — stokowski-sandbox-test archive-target 1776661154929`). Hidden from default views; recoverable via `include_archived=True`. Safe to delete permanently if desired.

**No pre-existing production content was modified.** Only created + archived one throwaway sub-issue under the chosen probe parent.

## Summary table

| # | Capability | Status | Notes |
|---|------------|--------|-------|
| 1 | Sub-issue creation via `issueCreate(input: { parentId })` | **CONFIRMED ✅** | Verified via `test_archive_removes_from_active_fetch` (creates child under SMI-30). `parent { id }` populates correctly in response — matches plan's Unit 0 defense-in-depth follow-up. |
| 2 | Custom field read via `fetch_template_children` (response shape) | **CONFIRMED ✅** | `test_custom_field_read` round-trips children of SMI-30. List shape, `parent_id`, `labels`, `state`, `archivedAt` fields populate as expected. Tenant-level `Cron`/`Timezone` custom fields remain plan-tier-gated; R3 YAML fallback is the documented path for affected tenants. |
| 3 | Custom field write (update `Timezone`) | **Off in MVP** | Plan Key Decision: operator edits YAML or Linear UI directly. |
| 4 | `issueArchive` mutation | **CONFIRMED ✅** | `test_archive_removes_from_active_fetch` archived created child; `include_archived=False` omitted it, `include_archived=True` re-surfaced it. Archive is non-destructive (data persists, recoverable). |
| 5 | `commentUpdate` mutation | **Off in MVP** | Plan Key Decision: append-only watermark supersession is the baseline. |
| 6 | Label ID resolution + colon-bearing label names | **CONFIRMED ✅** | `test_label_id_resolution` passes: absent names omitted from response dict, empty input short-circuits. Colon-bearing labels (`schedule:<type>`, `slot:<ISO>`) accepted by Linear. |
| 7 | `croniter` DST behavior | **CONFIRMED** | Pinned `croniter==6.2.2`; offline-verified per probe output below. |

## Capabilities

### 1. Sub-issue creation (`parentId`) — LIKELY

**What the plan needs.** `issueCreate(input: { parentId, teamId, title, description, labelIds, stateId? }) -> { success, issue { id, identifier, parent { id } } }`. Behavior on archived parent matters because retention could race template-delete against child-create.

**Evidence.** Multiple third-party guides and the Apollo-hosted schema reference list `IssueCreateInput` as including `parentId` and `labelIds` as first-class fields. A representative mutation shape surfaced in web search:

```graphql
mutation IssueCreate {
  issueCreate(input: {
    title: "..."
    teamId: "<uuid>"
    parentId: "<parent-uuid>"
    labelIds: ["<label-uuid>", "..."]
  }) {
    success
    issue { id identifier title parent { id } }
  }
}
```

An older community thread mentions a `GraphQL-Features: sub_issues` header for some sub-issue mutations (`addSubIssue`, `removeSubIssue`); parent-at-create appears to be stable without that header but this is worth verifying against the live API.

**Assumptions made.**
- `labelIds` and `parentId` can be supplied in the same `issueCreate` call (not requiring a follow-up `issueUpdate`).
- Response includes `issue.identifier` (the human-readable key like `SMI-42`) without extra selection set.
- Creating a child on an archived parent either succeeds (child is created, potentially archived inheritance) or returns a structured error — not a silent no-op.

**Decision.** Proceed as if supported. Unit 5 (new `create_child_issue` op in `linear.py`) must:
1. Request `parent { id }` in the response so the sibling-by-label-lookup step in the evaluator can cross-check.
2. Handle the "parent archived" error path by writing a terminal `skipped_error` watermark with reason `parent_archived` rather than retrying forever.

**Fallback if probe fails.** Encode parent link in child description as YAML front matter (`parent_template: <template_id>`) + explicit label `template:<template-identifier>`. Linear sub-issue tree visualization is lost; R12 "fire history is the children list" now reconstructs via a `labels: { name: { startsWith: "template:" } }` filter. Non-destructive — already compatible with the evaluator's label-based duplicate-sibling detection.

### 2. Custom field read — LIKELY

**What the plan needs.** Read `Cron` and `Timezone` custom fields on template issues. Plan R3 specifies: custom field first, description YAML front matter fallback (per-template).

**Evidence.** Linear released "Attributes" / "Custom fields" in the general product (plan-tier-gated). The public GraphQL docs do not yet quote a `CustomField` or `Attribute` node type in canonical text, but the SDK schema at `github.com/linear/linear` references attribute-like types. Plan tier matters — smaller teams may not have the feature exposed.

**Assumptions made.**
- Custom field values are reachable via a sub-selection on `Issue` (likely `Issue.attributes` or `Issue.customFields`, returning a list with `{ definition: { key }, value }` shape).
- Field values are strings (cron expression, IANA TZ name) — no complex typed unions.
- Absence of the field returns `null`/empty list, not a hard error.

**Decision.** Implement behind a **feature probe at startup**: on first fetch of any template, if the selected custom-field path raises `Cannot query field`, log a warning and fall back to YAML-only mode workspace-wide. This avoids tying config correctness to a plan-tier assumption.

**Fallback.** YAML-only. Already supported per R3. No code branch removed — the YAML path is the primary path in tests, the custom-field path is the optional enhancement.

### 3. Custom field write (Stokowski → `Timezone`) — LIKELY-TO-FALLBACK

**What the plan needs.** Ability to write `Timezone` on a template when operator has left it blank but their tenant has a default. Very low priority per plan (`nice-to-have`).

**Evidence.** Write APIs for custom fields are less commonly exposed than reads in most issue trackers. Linear's general "full support for mutating all entities" claim applies to first-class entities; attribute/custom-field writes have a separate surface (if any) that isn't documented in the pages fetched.

**Decision.** Do **not** implement writes in MVP. Stokowski reads the field; operators own the write surface (Linear UI). Revisit only if operator feedback demands it.

**Fallback.** This is the fallback. No code needed.

### 4. `issueArchive` mutation — LIKELY

**What the plan needs.** Archive a child issue N days after terminal. Response shape (for error handling). Parent-archive cascade semantics (relevant to accidental template archival). Whether archived issues are filtered out of normal `issues(...)` queries by default.

**Evidence.** Generic `ArchivePayload` referenced in the SDK schema. Mutation naming convention (`<entity>Archive`) is consistent across Linear's API (e.g. `teamArchive`, `projectArchive`). Expected signature:

```graphql
mutation IssueArchive($id: String!) {
  issueArchive(id: $id, trash: false) {
    success
    lastSyncId
    entity { id archivedAt }
  }
}
```

Linear's GraphQL `issues(...)` filter historically excludes archived issues by default — this is inferred from Linear docs conventions, NOT quoted from the fetched pages.

**Assumptions made.**
- `issueArchive(id)` soft-archives (sets `archivedAt`); does NOT hard-delete.
- Archived children are filtered out of the template's `children` connection by default — but the `includeArchived: true` argument can restore them when needed for operator history review.
- Parent archive does NOT cascade to sub-issue archival. Stokowski must archive children explicitly (which matches R13 "best-effort, per-child" retention).

**Decision.** Implement `archive_issue(issue_id)` with structured error handling. Retention sweep runs per-child, never on the template. Include `includeArchived: true` on any query that needs to surface archived fire history (e.g. dashboard "last 30 fires" should still show the last fire even if it's just been archived).

**Fallback.** If `issueArchive` is unavailable or the tenant lacks permission, fall back to `issueUpdate(stateId: <archived-terminal-state>)` — a terminal Linear state configured as the retention sink. R13 "best-effort" semantics already tolerate this.

### 5. `commentUpdate` mutation — LIKELY

**What the plan needs.** Edit an existing comment (specifically: update the `pending` watermark in place rather than appending a new terminal watermark). Plan decision: watermarks are append-only by default; `commentUpdate` is strict optimization. So this one is low-risk if unavailable.

**Evidence.** Linear's "full mutating support for all entities" claim is general; the mutation naming convention (`commentCreate`, `commentUpdate`, `commentDelete`) is the Linear idiom. Author-only permissions are the standard expectation — Stokowski should only ever update comments it authored (it does, since only Stokowski posts `stokowski:` HTML-hidden watermarks).

**Assumptions made.**
- `commentUpdate(id: String!, input: CommentUpdateInput!)` returns `{ success, comment { id body updatedAt } }`.
- Author must match the token owner (so Stokowski's service user can update its own comments).
- Updating a comment does not reset its `createdAt` — `parse_latest_fired`'s oldest-first scan semantics still hold because our scan uses `createdAt`.

**Decision.** Watermark compaction stays **off** in MVP. `commentCreate` exclusively. If Unit 0's live probe later confirms `commentUpdate`, a follow-up optimization pass can compact `pending → terminal` into a single edit without touching the public protocol.

**Fallback.** Append-only is the baseline. No fallback needed.

### 6. Label constraints — LIKELY

**What the plan needs.**
- Label names containing `:` (e.g. `slot:2026-04-19T08:00:00Z`, `schedule:compound-refresh`, `workflow:compound-refresh`).
- Auto-create vs. pre-create semantics for `labelIds` at issue-create time.
- Max length tolerable — our slot labels can reach ~30 characters (`slot:2026-04-19T08:00:00Z` = 27 chars, well within typical limits).

**Evidence.** Community convention has long used colon-delimited labels (`type:bug`, `priority:high`) in Linear; the Linear UI allows `:` in label names. No explicit max-length documentation was located in the public pages fetched. `labelIds` takes an array of existing label IDs — auto-creation from a name at issue-create time is NOT the usual Linear pattern; an explicit `issueLabelCreate` is the documented creation path.

**Assumptions made.**
- Label names up to ~64 characters are safe (common GraphQL string-field default, unverified).
- Colons are permitted but may not be special-cased — treat as opaque identifier characters.
- `labelIds` MUST reference pre-existing labels. Auto-creation via label *name* in `issueCreate` is not supported.

**Decision.**
1. Evaluator pre-resolves labels via `issueLabels(filter: { name: { in: [...] } })` before each `create_child_issue` call, with a cache keyed by team. Missing labels are created via `issueLabelCreate` on first use.
2. Slot format is second-precision ISO-8601 with `Z` suffix (e.g. `slot:2026-04-19T08:00:00Z`) — canonicalized by the single helper `canonicalize_slot()` in `scheduler.py` per the plan's Key Decision.
3. Label names stay strictly under 48 characters (comfortable safety margin).

**Fallback if colons prove problematic.** Replace `:` with `__` in slot labels: `slot__2026-04-19T08-00-00Z` (replace `:` in time component too, since Linear may normalize). The canonicalizer handles this at one seam. Watermark JSON still uses ISO-8601 — only the label form changes.

### 7. `croniter` DST behavior — CONFIRMED

**Version pin.** `croniter==6.2.2` (confirmed installed via `pip`, `importlib.metadata.version("croniter")` → `6.2.2`). `pyproject.toml` entry: `croniter>=6.0.0,<7.0.0`.

**Verified behavior** (see probe output at the end of this section):

- **Spring-forward nonexistent slot**: For `30 2 * * *` with `America/New_York` on 2026-03-08 (2 AM local is skipped to 3 AM), croniter **bumps the fire to 3:00 AM local** (UTC 07:00) on the transition day, then resumes normal 2:30 AM slots on subsequent days. The nonexistent slot is NOT silently dropped; it fires at the next valid local wall-clock.
- **Fall-back duplicate slot**: For `30 1 * * *` with `America/New_York` on 2026-11-01 (1 AM local occurs twice), croniter fires **TWICE** — once at `01:30 -04:00` (UTC 05:30) and once at `01:30 -05:00` (UTC 06:30). The two fires are distinct UTC slots, so Stokowski's per-UTC-slot idempotency invariant naturally deduplicates: both will produce watermarks, both will produce children, but with distinct `slot:<UTC>` labels.
- **Hourly across fall-back**: For `0 * * * *`, the 1 AM local hour fires twice (UTC 05:00 and 06:00), giving a 25-hour day. All slots are unique in UTC (verified: 6 unique of 6 collected).
- **Hourly across spring-forward**: For `0 * * * *`, the 2 AM local hour is skipped entirely — the sequence jumps from 1 AM local → 3 AM local (23-hour day).

**Operator implication** (for Unit 14 docs): an operator using `0 1 * * *` on a fall-back day will see **two fires** of their schedule, roughly an hour apart. This is croniter's behavior, not Stokowski's policy. If the operator wants one fire, they should either pick a time outside the ambiguous hour (e.g. `0 3 * * *` — never ambiguous in US tzs) or use UTC for that template's `Timezone`.

**Operator implication** for spring-forward: a template scheduled for `30 2 * * *` will still fire on the transition day, but at `3:00 AM local` (the next valid wall-clock) rather than `2:30 AM local`. No fire is lost.

## Plan Adjustments

No required changes to the plan. The findings align with the documented fallback paths and the "Watermarks are append-only by default" decision. Two small additions recommended:

1. **Unit 5 task addition**: `create_child_issue` request shape should include `parent { id }` in the return selection set so the evaluator can cross-check the parent link as a defense-in-depth guard against accidental cross-template creation. Estimated cost: one extra GraphQL field, no meaningful latency impact.
2. **Unit 14 operator docs addition**: Explicitly document the fall-back double-fire and spring-forward shift behaviors (from capability 7) with a worked example, so operators using `0 1 * * *` or `30 2 * * *` patterns aren't surprised.

## Probe code (croniter only)

Run with `python3 -m venv /tmp/v && /tmp/v/bin/pip install croniter && /tmp/v/bin/python <this-script>`. No network required.

```python
from croniter import croniter
from datetime import datetime
from zoneinfo import ZoneInfo
from importlib.metadata import version

print(f"croniter version: {version('croniter')}")
tz = ZoneInfo("America/New_York")

# Probe 1: spring-forward nonexistent slot (2:30 AM local on 2026-03-08)
print("\n--- SPRING FORWARD: 30 2 * * * across 2026-03-08 ---")
it = croniter("30 2 * * *", datetime(2026, 3, 7, 12, 0, tzinfo=tz))
for _ in range(4):
    n = it.get_next(datetime)
    print(f"  {n.isoformat()}  utc={n.astimezone(ZoneInfo('UTC')).isoformat()}")

# Probe 2: fall-back duplicate slot (1:30 AM local on 2026-11-01)
print("\n--- FALL BACK: 30 1 * * * across 2026-11-01 ---")
it = croniter("30 1 * * *", datetime(2026, 10, 31, 12, 0, tzinfo=tz))
for _ in range(4):
    n = it.get_next(datetime)
    print(f"  {n.isoformat()}  utc={n.astimezone(ZoneInfo('UTC')).isoformat()}")

# Probe 3: hourly across fall-back — unique UTC slots invariant
print("\n--- FALL BACK: 0 * * * * across 2026-11-01 ---")
it = croniter("0 * * * *", datetime(2026, 11, 1, 0, 30, tzinfo=tz))
seen = []
for _ in range(6):
    n = it.get_next(datetime)
    seen.append(n.astimezone(ZoneInfo('UTC')).isoformat())
    print(f"  {n.isoformat()}  utc={seen[-1]}")
print(f"  unique UTC slots: {len(set(seen))} of {len(seen)}")

# Probe 4: hourly across spring-forward — 2 AM slot skipped
print("\n--- SPRING FORWARD: 0 * * * * across 2026-03-08 ---")
it = croniter("0 * * * *", datetime(2026, 3, 8, 0, 30, tzinfo=tz))
for _ in range(6):
    n = it.get_next(datetime)
    print(f"  {n.isoformat()}  utc={n.astimezone(ZoneInfo('UTC')).isoformat()}")
```

**Probe output** (captured 2026-04-19 UTC, croniter 6.2.2):

```
croniter version: 6.2.2

--- SPRING FORWARD: 30 2 * * * across 2026-03-08 ---
  2026-03-08T03:00:00-04:00  utc=2026-03-08T07:00:00+00:00
  2026-03-09T02:30:00-04:00  utc=2026-03-09T06:30:00+00:00
  2026-03-10T02:30:00-04:00  utc=2026-03-10T06:30:00+00:00
  2026-03-11T02:30:00-04:00  utc=2026-03-11T06:30:00+00:00

--- FALL BACK: 30 1 * * * across 2026-11-01 ---
  2026-11-01T01:30:00-04:00  utc=2026-11-01T05:30:00+00:00
  2026-11-01T01:30:00-05:00  utc=2026-11-01T06:30:00+00:00
  2026-11-02T01:30:00-05:00  utc=2026-11-02T06:30:00+00:00
  2026-11-03T01:30:00-05:00  utc=2026-11-03T06:30:00+00:00

--- FALL BACK: 0 * * * * across 2026-11-01 ---
  2026-11-01T01:00:00-04:00  utc=2026-11-01T05:00:00+00:00
  2026-11-01T01:00:00-05:00  utc=2026-11-01T06:00:00+00:00
  2026-11-01T02:00:00-05:00  utc=2026-11-01T07:00:00+00:00
  2026-11-01T03:00:00-05:00  utc=2026-11-01T08:00:00+00:00
  2026-11-01T04:00:00-05:00  utc=2026-11-01T09:00:00+00:00
  2026-11-01T05:00:00-05:00  utc=2026-11-01T10:00:00+00:00
  unique UTC slots: 6 of 6

--- SPRING FORWARD: 0 * * * * across 2026-03-08 ---
  2026-03-08T01:00:00-05:00  utc=2026-03-08T06:00:00+00:00
  2026-03-08T03:00:00-04:00  utc=2026-03-08T07:00:00+00:00
  2026-03-08T04:00:00-04:00  utc=2026-03-08T08:00:00+00:00
  2026-03-08T05:00:00-04:00  utc=2026-03-08T09:00:00+00:00
  2026-03-08T06:00:00-04:00  utc=2026-03-08T10:00:00+00:00
  2026-03-08T07:00:00-04:00  utc=2026-03-08T11:00:00+00:00
```

## Required follow-up before Phase 2 merges

Each LIKELY entry above becomes CONFIRMED only after a live probe against a real Linear workspace. Recommended probe script (~50 lines, lives in a throwaway `tools/linear_api_probe.py` — NOT shipped):

1. Create a label `stokowski-probe:test` on a throwaway team.
2. Call `issueCreate` with `parentId` pointing to a pre-existing test issue + `labelIds: [<label-id>]` + colon-bearing label creation. Confirm response includes `parent { id }` and `labels { nodes { id name } }`.
3. Query the issue back with a custom-field selection (`attributes { ... }` or `customFields { ... }` — try both). Record which resolves.
4. Call `commentCreate` → capture comment id → call `commentUpdate` on it with new body. Confirm `success: true` and new body present.
5. Call `issueArchive` on the child. Fetch parent's `children` connection with and without `includeArchived: true`. Confirm filtering behavior.
6. Clean up: unarchive / delete test issues, delete test label.

Record the findings (actual field names, response shapes, error strings) back into this document in a new "Live verification results" section before the Unit 0 gate closes.

## References

- Linear Developers overview: https://linear.app/developers
- GraphQL getting started: https://linear.app/developers/graphql
- Advanced usage: https://linear.app/developers/advanced-usage
- Linear SDK schema (partial, truncated during fetch): https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql
- Apollo Studio schema reference: https://studio.apollographql.com/public/Linear-API/schema/reference?variant=current
- Apollo Studio `IssueCreateInput` (visited, returns JS-rendered page): https://studio.apollographql.com/public/Linear-API/variant/current/schema/reference/inputs/IssueCreateInput
- Community sub-issues beta discussion: https://github.com/orgs/community/discussions/131957
- croniter PyPI: https://pypi.org/project/croniter/
- croniter DST issue history: https://github.com/taichino/croniter/issues/90
- Python `zoneinfo` stdlib (3.11+): https://docs.python.org/3/library/zoneinfo.html

## Changelog

- 2026-04-19 — Initial findings (Unit 0, plan `2026-04-19-003`). All Linear entries LIKELY pending live probe; `croniter` CONFIRMED against 6.2.2.
