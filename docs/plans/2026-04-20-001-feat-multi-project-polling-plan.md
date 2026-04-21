---
title: "feat: Multi-project polling support"
type: feat
status: active
date: 2026-04-20
origin: docs/brainstorms/2026-04-20-multi-repo-support-requirements.md
---

# feat: Multi-project polling support

## Overview

Extend Stokowski to poll N Linear projects from a single orchestrator process by binding multiple `workflow.<project>.yml` files to one `Orchestrator` instance. Each file remains a complete, self-contained `ServiceConfig` — no nested config schema, no cross-file coupling. The orchestrator multiplexes: `self.configs: dict[project_slug, ParsedConfig]` + a per-issue `_issue_project: dict[issue_id, project_slug]` cache that routes every existing config lookup to the correct file via a `_cfg_for_issue(issue_id)` helper.

Functionally, this mirrors N independent orchestrator processes — each project has its own workflow, repos registry, hooks, `tracker.api_key`, and Linear fetch — but shares one daemon, one dashboard, one dispatch budget, and one workspace root hierarchy.

## Problem Frame

Today a Stokowski instance polls exactly one Linear project. Teams running multiple products in multiple Linear projects have to either run N daemons (losing unified visibility) or consolidate into one project (losing team boundaries). Both options contradict the operating model the multi-repo brainstorm targeted: "team-affine board spanning multiple repos." A team may also run multiple Linear projects (development + research + customer-support, for example) and want one orchestrator serving all of them.

This plan delivers that foundational capability: **one orchestrator → N Linear projects → each project has its own workflow and repos registry**. The AWS multi-tenant future (see origin) is a separate, larger piece of work that builds on this primitive; nothing here depends on or anticipates it.

## Requirements Trace

- **R1.** CLI accepts a single workflow file (legacy), a directory, a glob, or an explicit list of files. Each file supplies a complete `ServiceConfig` with its own `tracker.project_slug`.
- **R2.** Each project file is loaded, validated, and hot-reloaded independently. A broken file for one project does not stop dispatch for healthy projects.
- **R3.** Each project uses its own `LinearClient` instance (constructed from that project's `tracker.endpoint` + `resolved_api_key()`), so per-project API keys and rate-limit budgets stay isolated.
- **R4.** Every per-issue config lookup routes through a single `Orchestrator._cfg_for_issue(issue_id)` helper that reads `self._issue_project[issue_id]` and returns the owning `ServiceConfig`. Failure to resolve the project for a known issue is a bug — dispatch never silently falls back to a different project's config.
- **R5.** Issue→project binding is established once, at fetch time, and flows through every downstream operation (dispatch, prompt assembly, tracking comments, workspace cleanup, cold-start recovery).
- **R6.** Config settings split three ways:
  - *Shared globals, first-file-wins:* `agent.max_concurrent_agents` (the global dispatch budget), `server.port` (one dashboard port).
  - *Shared globals, reduced across files:* `polling.interval_ms` — effective value is `min()` across all loaded files, so the tightest interval wins. Not first-file-wins.
  - *Per-project (isolated by issue.id):* `workspace.root`, `hooks`, `docker.*`, `claude` defaults, `linear_states`, `states`, `workflows`, `repos`, `agent.max_concurrent_agents_by_state` (state names are per-project — primary-config's per-state limits cannot apply across projects because state names differ per workflow).
- **R7.** Agent subprocess env adds `STOKOWSKI_LINEAR_PROJECT_SLUG` per-dispatch alongside the existing `STOKOWSKI_ISSUE_IDENTIFIER` and `STOKOWSKI_REPO_NAME`.
- **R8.** `get_state_snapshot()` exposes `project_slug` on every `running` / `gates` / `retrying` entry so the dashboard and any MCP-consuming agent can see which project each issue belongs to.
- **R9.** Backward compatibility: a single `workflow.yaml` passed as a positional argument continues to work identically to pre-branch behavior. The single-file case is a multi-project config with one project.
- **R10.** Configuration validation catches cross-project mistakes: two files sharing the same `tracker.project_slug` is a startup error; a directory containing zero valid workflow files is a startup error.
- **R11.** `_is_eligible` and `_handle_retry` resolve active/terminal Linear state names from the issue's project config, not a shared global — Linear state names can legitimately differ per project.
- **R12.** Startup cleanup iterates per-project. Docker orphan volume pruning unions active keys across all projects. Per-project hook rendering still applies (R6 from the multi-repo brainstorm).

## Scope Boundaries

- **Not in scope:** AWS multi-tenant orchestration (`tenants.yaml`, per-tenant Fargate tasks, centralized registry). This plan is single-process, operator-deployed. The foundational primitive lands here; the hosted MVP is a separate brainstorm/plan.
- **Not in scope:** Per-project `max_concurrent_agents` budgets. The dispatch budget is global; projects compete for slots. Operators who need per-project fairness can wait for a follow-up; the core primitive ships with shared-global semantics.
- **Not in scope:** Per-project dashboard filtering/grouping in the web UI beyond exposing `project_slug` on each entry. Dashboard HTML/JS changes stay minimal (existing single-panel view, now annotated with project). Richer per-project panels are a separate UI pass.
- **Not in scope:** Dependency relationships between projects (e.g., "issue A in project X blocks issue B in project Y"). Linear issue blockers already cross-reference; no multi-project-specific logic.
- **Not in scope:** Shared repo registry across projects. Each project's `repos:` is independent. If two projects target the same repository, each declares it in its own config.

### Deferred to Separate Tasks

- **Per-project concurrency fairness:** Future follow-up when operator demand appears. A small addition layered on top; not required for the foundational capability.
- **Top-level `orchestrator.yml` for true global settings:** Currently "first file wins" is a compromise. If operators report friction, a separate small config file for global settings can be added without touching the per-project workflow files.

## Context & Research

### Relevant Code and Patterns

- `stokowski/orchestrator.py` — the orchestration hub. ~136 `self.cfg.*` read sites (plus indirect reads through helpers — see Unit 5) today; the planning research identified a clean split between pre-dispatch (no issue context) and post-dispatch (issue.id available) reads. Post-dispatch sites become `self._cfg_for_issue(issue.id).*`; pre-dispatch sites iterate over `self.configs.values()` or pick the primary config.
- `stokowski/main.py` — `cli()` takes a single positional `workflow` argument with `nargs="?"`. Extension: accept `nargs="*"` OR a directory; expand into a list. `Orchestrator.__init__` signature broadens from `workflow_path: str | Path` to `workflow_paths: Sequence[str | Path] | str | Path`.
- `stokowski/config.py` — `parse_workflow_file` returns a `ParsedConfig`. No changes to file-level parsing; each project file parses as today.
- `stokowski/linear.py` — `LinearClient` constructor takes `(endpoint, api_key)`. Per-project client map: `self._linear_clients: dict[str, LinearClient]`.
- **Multi-repo pattern reuse** — the `_issue_repo`/`_issue_workflow` cache-with-three-tier-fallback pattern is the exact shape for `_issue_project`. Cleanup parity requirement (CLAUDE.md pitfalls) applies.
- **`_run_worker` hot-reload snapshot (ADV-005)** — `cfg = self.cfg` at worker entry is already the pattern. The multi-project version becomes `cfg = self._cfg_for_issue(issue.id)` at worker entry; the snapshot discipline stays identical.

### Institutional Learnings

`docs/solutions/` does not exist yet. No prior solutions to draw on. This plan, once shipped, is a candidate seed entry.

### External References

No external research needed. The codebase has strong local patterns for the multi-cache, per-issue-routing shape (see `_issue_repo`, `_issue_workflow` in the multi-repo work).

## Key Technical Decisions

- **One file per project, no nested config schema.** Each `workflow.<project>.yml` remains a complete `ServiceConfig`. This preserves the entire existing config parser, validation, and schema surface. Rejected alternatives: nested `projects:` key (major refactor), top-level orchestrator config with per-project sub-configs (two config shapes, two validation paths, twice the surface area). The file-per-project approach is what the user explicitly chose as the simplest path.
- **Per-issue project binding via `_issue_project` cache.** Set once at fetch time; read by `_cfg_for_issue(issue_id)`; cleared by `_cleanup_issue_state`. Mirrors the existing `_issue_workflow` / `_issue_repo` pattern exactly.
- **Per-project `LinearClient`.** Different projects can have different API keys and even different endpoints (unusual but supported). One client per project, constructed lazily via `_linear_client_for(project_slug)`.
- **Shared global dispatch budget.** `agent.max_concurrent_agents` taken from the primary (first-loaded) config applies across all projects. Projects compete for slots. Simplifies the tick loop dramatically vs. per-project budgets and matches what operators likely want ("I have 10 agent slots, distribute them where work exists").
- **Polling interval = min across files.** A project that polls every 5 seconds forces the whole orchestrator onto the 5-second cadence. The fastest-polling project's interval wins; others just poll more often than strictly necessary (harmless). Note: this is NOT first-file-wins. `_load_all_workflows()` computes the min at load time and caches it; the `start()` poll loop reads the cached min, not `_primary_cfg().polling.interval_ms`.
- **Per-file hot-reload, per-file error isolation.** Each tick iterates `self.configs` and re-parses each file independently. A broken file keeps its last-known-good `ParsedConfig` in `self.configs[slug]` and logs an error for that slug only. Healthy projects continue to dispatch.
- **Backward compatibility is first-class.** Passing a single `workflow.yaml` path produces a `self.configs` dict with one entry. Every code path that handles N projects is the same code that handles 1 project. No "legacy mode" branching.
- **Cross-project issue identifier collisions are impossible.** Linear issue IDs (the GraphQL `id` field) are globally unique UUIDs. Team-prefixed identifiers (`SMI-14`, `WEB-22`) are globally unique within a Linear workspace. All per-issue caches stay keyed by `issue.id` with no project component.

## Open Questions

### Resolved During Planning

- **Which file supplies shared globals?** The first-loaded (alphabetical) file. Rationale: predictable, deterministic, doesn't require a new config layer. If operators report friction, add `orchestrator.yml` as a follow-up.
- **What happens when all project files are invalid at startup?** Orchestrator refuses to start with a clear error listing each file's validation failures. Same behavior as today for a single broken file, extended across N.
- **What if two files declare the same project_slug?** Startup validation error. Listed in the CLI output alongside per-file validation errors.
- **Directory vs explicit list vs glob at the CLI?** Accept all three, per user direction. Implementation: if the single argument is a directory, enumerate `*.yml` / `*.yaml` in it; if it's a glob, expand; if it's a file, treat as a one-entry list; if multiple positional args, use them as the explicit list.
- **`LINEAR_PROJECT_SLUG` env var already set today.** The existing `agent_env()` / `docker_env()` in `ServiceConfig` already injects this from `self.tracker.project_slug`. For multi-project, this stays correct because the env builder is called on the issue's project config (via the `cfg` snapshot). New var `STOKOWSKI_LINEAR_PROJECT_SLUG` is added for symmetry with the other `STOKOWSKI_*` vars.
- **Dry-run (`--dry-run`) with multiple files.** Iterates each project file, validates independently, prints a per-project status block. Existing `--dry-run` output structure extends cleanly.

### Deferred to Implementation

- **Exact signature for the CLI change.** Whether `nargs="*"` with post-processing or a separate `--workflow-dir` flag is cleaner. Implementation experiments with the flow; readability wins over cleverness.
- **Exact shape of the per-file config-loading function.** Likely a small helper `_load_project_configs(paths: list[Path]) -> dict[slug, ParsedConfig]` that's shared between startup and tick-time reload. Determined at implementation by what makes the tick-loop cleanest.
- **Whether `_primary_cfg()` is a method or a cached property.** Decide during the `self.cfg.X` sweep — the helper name is settled (`_primary_cfg`), the question is whether to recompute each call or cache at load time.
- **Alphabetical sort of discovered files.** Required for "first-loaded" determinism. Implementation uses `sorted(paths)`; specifics of locale/case handling are platform-default.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

**Orchestrator state multiplexing (Python-ish sketch):**

```
class Orchestrator:
    def __init__(self, workflow_paths: Sequence[Path] | Path):
        self.workflow_paths: list[Path] = _resolve(workflow_paths)
        # Keyed by project_slug; ordered by load order (first = primary)
        self.configs: dict[str, ParsedConfig] = {}
        self._linear_clients: dict[str, LinearClient] = {}
        # Binds every known issue.id to the project_slug that owns it
        self._issue_project: dict[str, str] = {}
        # ...all existing per-issue caches unchanged, keyed by issue.id...

    def _cfg_for_issue(self, issue_id: str) -> ServiceConfig:
        slug = self._issue_project.get(issue_id)
        if slug is None:
            raise RuntimeError(f"No project binding for {issue_id}")
        return self.configs[slug].config

    def _primary_cfg(self) -> ServiceConfig:
        return next(iter(self.configs.values())).config

    def _linear_client_for(self, project_slug: str) -> LinearClient:
        if project_slug not in self._linear_clients:
            cfg = self.configs[project_slug].config
            self._linear_clients[project_slug] = LinearClient(
                endpoint=cfg.tracker.endpoint,
                api_key=cfg.resolved_api_key(),
            )
        return self._linear_clients[project_slug]
```

**Tick fetch flow:**

```
_tick():
  _load_all_workflows()           # per-file reload, independent errors
  for slug, parsed in self.configs.items():
    client = self._linear_client_for(slug)
    issues = await client.fetch_candidate_issues(
      parsed.config.tracker.project_slug,
      parsed.config.active_linear_states(),
    )
    for issue in issues:
      self._issue_project[issue.id] = slug
      all_candidates.append(issue)
  # Sort, rejection pre-pass, eligibility, dispatch — unchanged semantics,
  # just routes each issue through _cfg_for_issue(issue.id) at every
  # config-read site.
```

**Backward-compat shape:**

```
stokowski workflow.yaml                  # one file → self.configs = {slug: ParsedConfig}
stokowski workflows/                     # directory → self.configs = {slug_a, slug_b, ...}
stokowski workflow.api.yml workflow.web.yml    # explicit list
stokowski 'workflow.*.yml'               # glob (shell-expanded or stokowski-expanded)
```

## Implementation Units

Grouped into four phases. Each phase lands as one logical commit.

### Phase A: Foundation (state model + discovery)

- [ ] **Unit 1: CLI + config discovery**

**Goal:** Accept one file, a directory, a glob, or multiple files from the command line. Normalize to a list of paths. Preserve single-file legacy behavior.

**Requirements:** R1, R9

**Dependencies:** None

**Files:**
- Modify: `stokowski/main.py` — `cli()` argparse shape, `run_orchestrator()` signature
- Test: `tests/test_cli_discovery.py` (new)

**Approach:**
- Extend the positional `workflow` argument to `nargs="*"` so users can pass 0-N paths.
- If zero args → existing auto-detection probes `./workflow.yaml`, `./workflow.yml`, etc. (unchanged).
- If one arg pointing at a file → one-entry list (legacy behavior preserved byte-for-byte).
- If one arg pointing at a directory → enumerate `*.yml` and `*.yaml` within, sort alphabetically, use those.
- If one arg containing glob metacharacters → expand via `glob.glob()`.
- If multiple args → use as explicit list directly (shell-expanded is fine too).
- Normalize to `list[Path]`, sorted, deduplicated by resolved absolute path.
- Display in the startup panel: show each file path with its parsed `project_slug` once loading succeeds.

**Patterns to follow:**
- Existing `cli()` argparse shape in `stokowski/main.py` at the CLI entry
- Existing auto-detection probe sequence (preserve it)

**Test scenarios:**
- Happy path: `stokowski workflow.yaml` → `workflow_paths == [Path("workflow.yaml")]`, one-entry list
- Happy path: `stokowski workflows/` with `workflow.a.yaml` + `workflow.b.yaml` → both paths in sorted order
- Happy path: `stokowski workflow.a.yaml workflow.b.yaml` → explicit two-path list
- Happy path: no args, auto-detect finds `./workflow.yaml` → single-entry legacy behavior
- Edge case: directory with no yaml files → clear startup error, not a silent empty list
- Edge case: path with shell glob (`workflow.*.yml`) resolved as a glob pattern
- Edge case: duplicate paths (same file passed twice, or once explicitly + once via glob) deduplicated by resolved absolute path

**Verification:** `stokowski --dry-run workflows/` with two example files prints one validation block per project. `stokowski --dry-run workflow.yaml` prints one block, byte-identical to pre-branch `--dry-run` output for the same file.

- [ ] **Unit 2: Orchestrator state refactor + `_cfg_for_issue`**

**Goal:** Replace `self.workflow: ParsedConfig | None` with `self.configs: dict[str, ParsedConfig]`. Add `_issue_project` cache, `_cfg_for_issue`, `_primary_cfg` helpers. Update `_cleanup_issue_state` parity. Update `__init__` to accept `Sequence[Path] | Path`.

**Requirements:** R4, R5, R6

**Dependencies:** Unit 1

**Files:**
- Modify: `stokowski/orchestrator.py` — `__init__`, `_cleanup_issue_state`, add helper methods
- Test: `tests/test_orchestrator_multi_project.py` (new)

**Approach:**
- Backward-compat constructor: accept a single `Path` / `str` and wrap into a one-entry list; accept a list/tuple of paths and use as-is.
- Add `self.configs: dict[str, ParsedConfig] = {}`, populated by `_load_all_workflows()` (Unit 3) — init body only declares the attribute.
- Add `self._issue_project: dict[str, str] = {}`. Parity: add `self._issue_project.pop(issue_id, None)` to `_cleanup_issue_state`. Update CLAUDE.md dict count from 15 → 16.
- Add `self._linear_clients: dict[str, LinearClient] = {}` to replace `self._linear: LinearClient | None`. Update `stop()` to iterate and close all.
- Keep a transitional `self.cfg` property that returns `self._primary_cfg()` so any not-yet-swept call site still works during the refactor. Mark it as deprecated internally; the Unit 5 sweep removes it.
- Helper methods:
  - `_cfg_for_issue(issue_id) -> ServiceConfig` — raises `RuntimeError` on unknown issue.id
  - `_primary_cfg() -> ServiceConfig` — returns first-loaded for shared globals
  - `_linear_client_for(project_slug) -> LinearClient` — lazy per-project client construction

**Patterns to follow:**
- `_issue_workflow` / `_get_issue_workflow_config` helper structure in `stokowski/orchestrator.py`
- `_cleanup_issue_state` parity discipline (CLAUDE.md pitfalls)

**Test scenarios:**
- Happy path: `Orchestrator([Path("a.yml"), Path("b.yml")])` stores both in `workflow_paths`
- Happy path: `Orchestrator(Path("single.yml"))` (legacy single-path form) wraps into a one-entry list
- Happy path: `_cfg_for_issue(issue_id)` with a populated `_issue_project` returns the right `ServiceConfig`
- Error path: `_cfg_for_issue(unknown_id)` raises `RuntimeError` (not silent fallback)
- Edge case: `_cleanup_issue_state(issue_id)` removes the `_issue_project` entry alongside all other per-issue tracking
- Integration: meta-test that every per-issue dict/set in `__init__` is popped by `_cleanup_issue_state` (extend the existing parity test from multi-repo work)

**Verification:** The transitional `self.cfg` property keeps the existing test suite passing. New tests confirm `_issue_project` cleanup parity and `_cfg_for_issue` semantics.

- [ ] **Unit 3: Per-file loading + independent hot-reload**

**Goal:** Load each file into `self.configs` with per-file error isolation. Replace `_load_workflow()` with `_load_all_workflows()` that iterates `self.workflow_paths`, re-parses each, and keeps the last-known-good config for any file that fails mid-run.

**Requirements:** R1, R2, R10

**Dependencies:** Unit 2

**Files:**
- Modify: `stokowski/orchestrator.py` — `_load_workflow()` → `_load_all_workflows()` plus per-file helper
- Test: `tests/test_orchestrator_multi_project.py`

**Approach:**
- `_load_all_workflows() -> dict[slug, list[str]]`: iterate each path, parse, validate, populate `self.configs[slug]` on success. Returns a dict of per-project error lists (empty list = healthy).
- On error, leave `self.configs[slug]` unchanged (so a hot-reload of a typo doesn't kill running agents). First-load errors (no prior good state) propagate to the caller via the error map and the orchestrator refuses to start.
- On success globally (no errors across any file), clear `self._config_blocked` (existing behavior — operator's fix resumes all blocked issues).
- **Cross-file validation — duplicate `project_slug` semantics:**
  - *At startup:* fatal error naming both file paths, orchestrator refuses to start.
  - *At hot-reload (mid-run):* the existing `self.configs` is NOT mutated (both entries stay as they were). The duplicate is logged at ERROR level every tick until operator resolves it (file paths named in the log). Running agents for the pre-duplicate slug continue with their pinned cfg snapshot. This is the "last-known-good" discipline extended to the duplicate case — do not evict either entry, because doing so could orphan in-flight dispatches.
- **File-removal semantics (`_linear_clients` + `self.configs` eviction):**
  - If a previously-loaded file disappears from the on-disk set on a hot-reload tick: the missing slug remains in `self.configs` until any running/retrying/gated issue for that slug has finished, at which point it's evicted. Concretely: `_cleanup_issue_state(issue_id)` for the last bound issue of a removed slug triggers a `_evict_project(slug)` that pops `self.configs[slug]`, closes and pops `self._linear_clients[slug]`. In-flight workers for the removed project complete on their pinned cfg snapshot. New fetches for that slug stop as soon as the file is gone (the per-project iteration skips it because it's no longer enumerated from the path discovery step).
  - If the file reappears before all issues drain: the reappearance goes through the normal parse/validate path and repopulates `self.configs[slug]`. `_linear_clients[slug]` stays closed (lazy re-construction on next `_linear_client_for(slug)` call).
- **Polling interval cache:** `_load_all_workflows()` computes `min(parsed.config.polling.interval_ms for parsed in self.configs.values())` and stores it on `self._polling_interval_ms` (or similar). The `start()` poll loop reads this cached value each tick so a hot-reload that changes the min is picked up on the next tick.
- **File ordering — case-insensitive sort:** when discovering files from a directory or deduplicating an explicit list, sort paths by `str(path).casefold()` (case-insensitive). This ensures `Workflow.api.yml` and `workflow.api.yml` don't reorder the "primary" designation across platforms with different case conventions. Document the rule in the startup log.
- **Startup logging:** after `_load_all_workflows()` succeeds, log a single structured block: for each loaded slug, log `file=<path>  slug=<slug>  primary=<bool>` so operators can immediately see the filename → project_slug mapping and which file supplies shared globals. On hot-reload, log only the delta (file added / file removed / slug's content changed).
- Display startup summary: per-project load status, repo counts, workflow counts, project_slug.

**Patterns to follow:**
- Existing `_load_workflow` error-return-list pattern
- `validate_config` accumulates errors rather than short-circuiting

**Test scenarios:**
- Happy path: two files, both parse cleanly → `self.configs` has two entries, error map is empty
- Happy path: hot-reload of a file with no changes → `self.configs` unchanged, no churn
- Happy path: hot-reload with an edit that validates → `self.configs[slug]` replaced
- Error path: first-load fails for one of two files → orchestrator refuses to start, error includes the slug
- Error path: hot-reload fails for one file, other healthy → orchestrator keeps last-good config for broken slug, continues dispatching for healthy slug, logs the broken slug's errors
- Error path: two files share `project_slug` → fatal startup error naming both file paths
- Error path: the primary (first-loaded) file fails hot-reload → healthy projects continue; primary_cfg-dependent global settings keep using last-good (documented behavior)

**Verification:** `stokowski --dry-run` on a two-file config with one valid and one invalid shows a clear per-project validation breakdown. Running on a healthy two-file config loads both without cross-contamination.

### Phase B: Runtime polling + dispatch

- [ ] **Unit 4: Per-project tick fetch loop + issue-to-project binding**

**Goal:** Update `_tick()` to iterate projects, fetch each with its own `LinearClient`, and bind each returned issue to its project in `_issue_project` before the downstream rejection pre-pass and dispatch.

**Requirements:** R3, R5, R11

**Dependencies:** Unit 3

**Files:**
- Modify: `stokowski/orchestrator.py` — `_tick()` fetch block, `_reconcile()` fetch calls, `_handle_gate_responses()` fetch calls
- Test: `tests/test_orchestrator_multi_project.py`, `tests/test_rejection_multi_project.py` (new)

**Approach:**
- `_tick()` iterates `self.configs.items()`. For each slug: `client = self._linear_client_for(slug)`; fetch candidates using that project's state names and project_slug; stamp `self._issue_project[issue.id] = slug` for each candidate; accumulate into a unified `all_candidates` list.
- Reconcile, rejection pre-pass, eligibility, dispatch — run once over the unified list. Every call into `_process_rejections`, `_is_eligible`, `_dispatch` reads per-issue config via `_cfg_for_issue(issue.id)` (Unit 5 sweep completes this).
- `_reconcile()` must now also iterate projects for its own fetches (gate issues, rework issues, terminal issues). Each reconciliation fetch uses the project-specific client + state names. **Per-issue state comparison requirement:** when the reconciler iterates multiple projects for gate/rework lookups, it MUST compare each returned issue's current state against the state names from THAT issue's project config (`_cfg_for_issue(issue.id).linear_states`), not a merged global set. Project A's "Review" state name differs from project B's; a merged set would falsely match state-equivalence across projects.
- **Cold-start ordering (critical):** `_handle_gate_responses()` runs for issues whose Linear state is `review` / `gate_approved` / `rework` — states which are NOT in `active_linear_states()` and therefore are NOT returned by the per-project candidate fetch in `_tick`. This means `_issue_project` is empty for these issues after restart. To resolve the circular dependency (need `project_slug` to call `_cfg_for_issue` → need `_cfg_for_issue` to identify which client to use → need the client to fetch the issue), the gate-handler MUST itself iterate projects:
  1. `for slug, parsed in self.configs.items():`
  2. `client = self._linear_client_for(slug)`
  3. Fetch issues in this project whose state is `review` / `gate_approved` / `rework` via `client.fetch_issues_by_states(parsed.config.tracker.project_slug, gate_states)`
  4. For each returned issue: `self._issue_project[issue.id] = slug` **before any call to `_cfg_for_issue` or `_resolve_current_state`**
  5. Then call the existing `_resolve_current_state` / comment-fetch / transition logic
  The binding-before-cfg-lookup ordering also applies to the rework branch, terminal-cleanup branch, and any other reconciliation path that may see a pre-bound issue ID. `_resolve_repo_for_coldstart` and `_resolve_gate_workflow` receive `issue` + `cfg` as arguments; they do not re-enter `_cfg_for_issue`, but their callers must have stamped `_issue_project` first.
- Retry fetches in `_handle_retry()` must resolve the project from `_issue_project[issue.id]` and use the project's client + active state names. Because retries are always scheduled post-dispatch, the `_issue_project` binding is guaranteed present; a missing binding for a retry target is a bug (fail-loud via the `_cfg_for_issue` RuntimeError).

**Execution note:** Add characterization coverage for the new fetch-loop shape before changing behavior — existing tests at `tests/test_state_machine.py` are the pattern.

**Patterns to follow:**
- Existing `_tick()` structure for fetch → reconcile → handle-gates → rejection pre-pass → dispatch
- `_process_rejections` async pre-pass pattern (multi-repo ADV-003 fix)

**Test scenarios:**
- Happy path: two projects, each returns 2 candidates → `_issue_project` has 4 entries, all 4 issues enter dispatch
- Happy path: one project is healthy, one's fetch fails → healthy project's candidates enter dispatch, failing project logs error + skipped this tick
- Edge case: an issue id seen in two projects' fetches on the same tick (shouldn't happen — Linear IDs are globally unique — but defensive): latest binding wins, warning logged
- Edge case: a project's `tracker.api_key` is invalid → `LinearClient` auth failure is caught per-project and logged, other projects continue
- Integration: two-project test with per-project repos; a dual-repo-labeled issue in project A triggers rejection pre-pass using project A's config; project B's issues are unaffected
- Integration: retry scheduled on a project-A issue → retry fetch goes through project-A's client, not any other project's client

**Verification:** Two-project integration test demonstrates independent fetch/dispatch without cross-project interference. Rejection pre-pass uses the correct project's Linear client for comment lookup.

- [ ] **Unit 5: `self.cfg.X` sweep → `_cfg_for_issue(issue.id).X` / `_primary_cfg().X`**

**Goal:** Sweep all ~136 `self.cfg.*` read sites (plus indirect reads through helpers — see Unit 5) in `stokowski/orchestrator.py`. Post-dispatch sites (issue.id in scope) become `self._cfg_for_issue(issue.id).*`. Pre-dispatch sites split between `self._primary_cfg().*` (shared globals) and iteration over `self.configs.values()` (operations that visit all projects).

**Requirements:** R4, R6, R11

**Dependencies:** Unit 2, Unit 4

**Files:**
- Modify: `stokowski/orchestrator.py` (the full file; ~136 targeted call-site changes)
- Test: extends existing `tests/test_state_machine.py` and `tests/test_rejection_coldstart.py` coverage implicitly

**Approach:**
- Categorize each call site:
  - **Post-dispatch (issue.id known):** `_run_worker`, `_dispatch`, `_transition`, `_enter_gate`, `_render_prompt_async`, `_render_prompt`, `_on_worker_exit`, `_resolve_current_state`, `_resolve_repo_for_coldstart`, `_resolve_gate_workflow`, `_get_issue_*_config`, `_handle_retry`, `_post_cancellation_comment`, `_post_hook_error_comment` — all use `self._cfg_for_issue(issue.id).X`.
  - **Pre-dispatch, shared global:** `start()` poll loop uses the cached min polling interval computed by `_load_all_workflows()` (not `_primary_cfg()`); slot budget in `_tick()` uses `self._primary_cfg().agent.max_concurrent_agents`; `server.port` in `start()` uses `self._primary_cfg().server.port`.
  - **Pre-dispatch, per-project iteration:** `_startup_cleanup`, `stop()` (docker cleanup check), `_reconcile()` fetch loops, `_handle_gate_responses()` fetch loops, Docker image pre-pull, `_cleanup_logs`.
- **`workflow_path.parent` sites (prompt file resolution):** any site that passes `workflow_dir` to `load_prompt_file()` or `assemble_prompt()` must resolve the parent of the owning project's file, not the first-loaded file's parent. These sites exist in `_render_prompt_async`, `_render_prompt`, prompt assembly called from `_run_worker`, and any hook-render helper that consumes `workflow_dir`. Replace bare `self.workflow_path.parent` references with `self.configs[self._issue_project[issue.id]].path.parent` (or a helper `_workflow_dir_for_issue(issue_id) -> Path`). Missing these causes a project-B agent to resolve its prompt files against project-A's directory — silent wrong-project prompt loading.
- **`_ensure_linear_client()` migration (22 call sites):** today every `_ensure_linear_client()` call returns the single `self._linear` instance. Each of these ~22 call sites falls into one of three shapes and must be explicitly categorized:
  - **Post-dispatch (issue.id known):** becomes `self._linear_client_for(self._issue_project[issue.id])` (or equivalently `self._linear_client_for(self._cfg_for_issue(issue.id).tracker.project_slug)`). Applies inside `_run_worker`, `_on_worker_exit`, `_transition`, `_enter_gate`, `_resolve_current_state`, `_handle_retry`, gate-response handlers operating on a bound issue.
  - **Pre-dispatch per-project iteration:** becomes `self._linear_client_for(slug)` inside a `for slug, parsed in self.configs.items():` loop. Applies to `_tick` candidate fetch, `_reconcile` gate/rework/terminal fetches, `_startup_cleanup` terminal-issue fetch, cold-start gate scan in `_handle_gate_responses`.
  - **Pre-dispatch shared-primary (rare):** any site that needs a single client reference without per-issue context (none should remain after Unit 4; if discovered, flag in review). None currently expected.
  The sweep must enumerate every `_ensure_linear_client()` call site, assign a shape, and migrate. The old helper is removed at the end of Unit 5.
- Remove the transitional `self.cfg` property introduced in Unit 2. Any remaining unqualified `self.cfg` is a bug and will fail at type-check / import time.
- `_run_worker` hot-reload snapshot (ADV-005): `cfg = self._cfg_for_issue(issue.id)` at worker entry, before any `await`. Identical semantics, per-project pinning.
- `_is_eligible` reads active/terminal state names from `self._cfg_for_issue(issue.id)` — a ticket whose project renames "In Progress" to "Working" must still pass eligibility.

**Execution note:** This unit is the largest mechanical change in the plan. Run the test suite after each logical cluster (helpers, dispatch, reconcile, gates) to keep the sweep grounded.

**Patterns to follow:**
- `_run_worker` cfg-snapshot pattern (multi-repo ADV-005 fix)
- `_get_issue_workflow_config` / `_get_issue_repo_config` three-tier fallback for hot-reload resilience

**Test scenarios:**
- Happy path: two-project dispatch runs through the full lifecycle — dispatch → prompt render → transition → cleanup — with each operation reading the correct project's config
- Happy path: single-project config (legacy) continues to pass all existing tests byte-for-byte
- Edge case: project A and project B have different `linear_states.active` values ("In Progress" vs "Working"). A ticket from project B passes `_is_eligible` when its state is "Working" (project B's name); project A's "In Progress" does not apply
- Edge case: project A has Docker enabled, project B doesn't. Dispatch in project A uses Docker; project B uses local subprocess. `stop()` cleanup iterates both appropriately.
- Integration: `_run_worker` cfg snapshot — during a worker's first `await`, hot-reload a DIFFERENT project's file; this project's cfg is unchanged; no behavior change for the in-flight worker
- Integration: rework path restores the correct project on cold-start recovery

**Verification:** All existing tests pass (baseline: 305 on the parent branch). After Unit 5 lands, grep for `self.cfg` in `stokowski/orchestrator.py` returns no matches — the transitional property introduced in Unit 2 is removed as part of this unit.

**Important grep caveat:** a clean grep only catches DIRECT `self.cfg.*` reads. It does NOT catch helper methods that internally hold `self.cfg` references (e.g., `_get_issue_workflow_config`, `_get_issue_repo_config`). The sweep must explicitly update these helpers to take `cfg` as an argument OR to call `_cfg_for_issue(issue_id)` / `_primary_cfg()` internally. After the sweep, the old `self.cfg` property is gone, so any helper that still reads `self.cfg` fails at import time — but helpers that use `self._primary_cfg()` when they should use `self._cfg_for_issue(issue_id)` silently route to the wrong project. Phase B test coverage must exercise project-B-specific behaviors (different `linear_states`, different `states`, different `repos`) to catch silent routing to the primary config.

### Phase C: Integration surfaces

- [ ] **Unit 6: Startup cleanup + Docker pre-pull iterate per-project**

**Goal:** `_startup_cleanup` iterates terminal issues per project. Docker volume orphan pruning unions active keys across all projects. Docker image pre-pull loops over all configured project images.

**Requirements:** R12

**Dependencies:** Unit 5

**Files:**
- Modify: `stokowski/orchestrator.py` — `_startup_cleanup()`, `start()` Docker pre-pull block
- Test: `tests/test_orchestrator_multi_project.py`

**Approach:**
- `_startup_cleanup()` outer loop per project: fetch that project's terminal issues via the project's client, iterate its repos, remove workspaces using its rendered hooks.
- **Mixed docker-enabled / docker-disabled handling:**
  - Docker volume orphan pruning is scoped to docker-enabled projects only. Build `active_keys` as the union of composite keys for `(issue, repo)` pairs from projects where `cfg.docker.enabled` is True. Projects with docker disabled contribute nothing to `active_keys` and are not involved in the volume-cleanup call.
  - If zero projects have docker enabled, skip the `cleanup_orphaned_volumes` call entirely.
  - If at least one project has docker enabled, call `cleanup_orphaned_volumes(active_keys)` once with the merged set.
  - **Hazard:** a project's volumes must not be pruned based on another project's `active_keys` view. Because `active_keys` always unions across ALL docker-enabled projects, volumes tagged by any enabled project are preserved. A filesystem-mode project cannot inadvertently delete a docker-mode project's volume (docker mode is never touched for it). A docker-mode project will preserve its own volumes regardless of another docker-mode project's activity level, because the union includes its own active keys.
- Docker image pre-pull at startup: union of `default_image` + all state-level `docker_image` + all repo-level `docker_image` across docker-enabled projects only. Skip entirely if no project has docker enabled.
- `stop()` docker-enabled check: iterate `self.configs.values()`; if ANY `cfg.docker.enabled` is True, run `cleanup_orphaned_containers()`. Skip otherwise.

**Patterns to follow:**
- Existing `_startup_cleanup` in `stokowski/orchestrator.py` (from the multi-repo branch; already iterates repos)
- `cleanup_orphaned_volumes` interface (takes an `active_keys: set[str]`)

**Test scenarios:**
- Happy path: two projects, each with one terminal issue → both workspaces removed at startup
- Happy path: two projects with different `docker.default_image` → both pre-pulled
- Edge case: only one of two projects has docker enabled → pre-pull + volume cleanup only run for the docker-enabled project; the other's cleanup takes the filesystem path
- Error path: one project's startup cleanup fails (Linear fetch error) → logged per-project, other project's cleanup continues
- Integration: stale workspace dir from project A is pruned; workspace dir from project B (different workspace root) is untouched

**Verification:** `stokowski --dry-run` on a two-project config logs per-project startup-cleanup status. Docker volume listing after startup shows orphan-free state for all projects.

- [ ] **Unit 7: Dashboard snapshot — `project_slug` on every entry + env-var injection**

**Goal:** Add `project_slug` field to `running`, `gates`, and `retrying` entries in `get_state_snapshot()`. Add `STOKOWSKI_LINEAR_PROJECT_SLUG` env var per-dispatch alongside `STOKOWSKI_ISSUE_IDENTIFIER`.

**Requirements:** R7, R8

**Dependencies:** Unit 5

**Files:**
- Modify: `stokowski/orchestrator.py` — `get_state_snapshot()`, env-var injection in `_run_worker`
- Modify: `stokowski/web.py` — dashboard HTML/JS to surface `project_slug` as a column or tag (minimal: just display the value; don't rebuild the UI)
- Test: `tests/test_state_snapshot.py` (new or extend existing), `tests/test_web_dashboard.py` if present

**Approach:**
- Snapshot: add `"project_slug": self._issue_project.get(r.issue_id)` to each `running` entry; same for `gates` and `retrying` entries.
- `RetryEntry` may need a `project_slug` field for the retrying section, OR the snapshot reads from `self._issue_project.get(e.issue_id)` directly (no RetryEntry change). Pick the simpler path at implementation.
- Env injection: after the existing `agent_env["STOKOWSKI_ISSUE_IDENTIFIER"] = issue.identifier` line in `_run_worker`, add `agent_env["STOKOWSKI_LINEAR_PROJECT_SLUG"] = self._issue_project[issue.id]`.
- Note the existing `LINEAR_PROJECT_SLUG` env (from `ServiceConfig.agent_env()`) already routes to the right value because `agent_env()` is called on the pinned per-issue cfg snapshot.
- Dashboard HTML/JS: minimal — add a `project_slug` field to the rendered row / card. Full UI revamp (filter by project, collapse per project) is out of scope.

**Patterns to follow:**
- Existing `get_state_snapshot()` shape in `stokowski/orchestrator.py`
- Existing `STOKOWSKI_ISSUE_IDENTIFIER` injection in `_run_worker`

**Test scenarios:**
- Happy path: single-project config → every entry has `"project_slug": "abc123"` (backward compat preserves the value even when there's only one project)
- Happy path: two projects with running issues → each entry correctly identifies its own project
- Edge case: legacy caller parsing the snapshot ignores unknown keys → `project_slug` addition is additive, no breaking change
- Integration: env dispatch — an agent subprocess reads `$STOKOWSKI_LINEAR_PROJECT_SLUG` and sees its project's slug

**Verification:** `GET /api/v1/state` includes `project_slug` on every entry. An agent hook script can read `$STOKOWSKI_LINEAR_PROJECT_SLUG` at dispatch time.

### Phase D: Tests + Docs

- [ ] **Unit 8: End-to-end integration tests**

**Goal:** Comprehensive integration test coverage for the multi-project scenarios. Augments the per-unit scenarios with full-tick, full-dispatch flows.

**Requirements:** R1-R12 (cross-cutting)

**Dependencies:** Units 1-7

**Files:**
- Create: `tests/test_multi_project_integration.py`
- Extend: `tests/test_rejection_coldstart.py` (multi-project cold-start case)
- Extend: `tests/test_state_machine.py` meta-parity test to include `_issue_project`

**Approach:**
- Fixture: two-project stub orchestrator with `_StubLinearClient` instances, one per project.
- Scenario 1: Dispatch isolation — issue in project A dispatches with project A's repos/workflow; project B remains idle.
- Scenario 2: Rejection isolation — dual-label issue in project A triggers rejection using project A's repos definitions; project B is untouched.
- Scenario 3: Cold-start recovery — orchestrator restart with in-flight issues across both projects; each issue recovers its repo and workflow from its own project's tracking comments.
- Scenario 4: Hot-reload isolation — broken file for project B during a tick; project A continues dispatching; fixing project B on next tick resumes both.
- Scenario 5: Shared dispatch budget — N slots total; if project A fills them, project B is queued on the next tick with free slots.
- Scenario 6: Backward compat — single-file config runs through the full test suite unchanged.
- Scenario 7: Config error — two files share `project_slug` → startup fails with a helpful message.

**Execution note:** These scenarios simulate the full `_tick` loop with stub clients. The stub pattern is well-established in `tests/test_rejection_coldstart.py` — extend it.

**Patterns to follow:**
- `_StubLinearClient` from `tests/test_rejection_coldstart.py`
- `_make_orch` fixture pattern from `tests/test_repos_config.py`

**Test scenarios:** (listed above under Approach)

**Verification:** `python -m pytest tests/` passes all 305+ existing tests plus the ~20 new integration tests. No regressions. `stokowski --dry-run workflows/` on a realistic two-project setup prints clean validation output.

- [ ] **Unit 9: Docs — CLAUDE.md, README.md, new example files**

**Goal:** Document the multi-project capability. Add worked examples. Update the architecture description.

**Requirements:** (cross-cutting)

**Dependencies:** Units 1-8

**Files:**
- Modify: `CLAUDE.md` — add "Multi-project polling" section; update `_cleanup_issue_state` dict count (15 → 16); add pitfall entries for per-project cfg routing and the `_cfg_for_issue` discipline
- Modify: `README.md` — add a collapsible "Multi-project orchestration" section under "What Stokowski adds"
- Create: `workflow.multi-project.example.yaml` (or similar naming) — worked example with two project stubs showing the full shape
- Modify: `workflow.example.yaml` (already split on the parent branch) — add a section pointing at the multi-project example

**Approach:**
- CLAUDE.md:
  - Under Key design decisions, add a short "Multi-project polling" subsection describing the one-file-per-project model.
  - Update pitfalls: (a) every new per-issue dict/set must land in `_cleanup_issue_state`; (b) any new `self.cfg.X` read must be routed via `_cfg_for_issue(issue_id)` for per-issue contexts or `_primary_cfg()` / iteration for global contexts.
  - Correct the dict count: was 15, now 16.
- README.md:
  - New section under "What Stokowski adds" with the headline, a short paragraph, and a command example.
  - Reference the new example file.
- Example file: a minimal but complete two-project config showing `tracker.project_slug`, per-project `repos:` (optional), distinct workspace roots, distinct Linear state name overrides.

**Test scenarios:**
- Happy path: `stokowski --dry-run workflows/` against the new example directory passes validation.
- *Test expectation: none for doc prose — this unit is documentation; no behavioral change.*

**Verification:** Documentation is internally consistent. The example file passes validation. README table of contents links resolve.

## System-Wide Impact

- **Interaction graph:** Every code path that previously read `self.cfg` — ~136 direct sites, plus indirect helper reads — is touched. Most direct sites are simple mechanical replacements. The dispatch loop and reconciliation fetch loops require structural changes (per-project iteration). Helper methods (`_get_issue_workflow_config`, `_get_issue_repo_config`) internally move from reading `self.cfg` to routing via `_cfg_for_issue(issue_id)` and are covered by Unit 5's explicit helper-migration requirement.
- **Error propagation:** Per-project errors are isolated: a broken file for one project does not block dispatch for healthy projects. Global errors (no files loaded, all files broken at startup) remain fatal.
- **State lifecycle risks:** `_issue_project` cleanup parity is the new parity risk. A missing `pop` in `_cleanup_issue_state` would leak entries after terminal-state cleanup — caught by the meta-test.
- **API surface parity:** `Orchestrator.__init__` signature broadens from single-path to path-or-list. All existing callers with a single path work unchanged. `LinearClient` is now map-keyed; callers of the internal `_ensure_linear_client()` become `_linear_client_for(project_slug)`.
- **Integration coverage:** Full-tick multi-project scenarios in Unit 8 — dispatch isolation, rejection isolation, cold-start recovery, hot-reload isolation, backward compat.
- **Unchanged invariants:**
  - All per-issue tracking dicts stay keyed by `issue.id` (globally unique).
  - `RunAttempt`, `Issue`, tracking comment payloads — no schema changes.
  - Workspace key composition (`{len}-{issue}-{repo}`) unchanged; Linear issue identifiers guarantee cross-project uniqueness.
  - Hot-reload clears `_config_blocked` on SUCCESS (existing behavior); extends to per-file success independently.

## Risks & Dependencies

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| The `self.cfg.X` sweep (Unit 5) misses a site → silent cross-project data corruption | Medium | High | Remove the transitional `self.cfg` property at the end of Unit 5. Any missed site fails at import/type-check time. Grep-verify the file after the sweep. |
| Primary-config selection surprises operators ("why does this setting from file B not apply?") | Medium | Low | Document the "first-loaded wins for globals" rule in CLAUDE.md and README. Log the selected primary file at startup with its slug. |
| Hot-reload race with concurrent `_run_worker` (ADV-005 extension) | Low | Medium | The existing `cfg = self.cfg` snapshot pattern at `_run_worker` entry becomes `cfg = self._cfg_for_issue(issue.id)`. Test coverage extends to hot-reload of a DIFFERENT project's file than the running worker's. |
| Shared dispatch budget starves one project under load from another | Medium | Medium | Document the shared-budget decision. Emit a WARN-level log when, at dispatch time, the slot budget is exhausted AND there exists at least one eligible issue from a project with zero currently-running issues. The log includes the starved project_slug and the count of eligible-but-unstarted issues. This gives operators a visible signal before they open a follow-up. If operators hit real starvation in practice, follow-up with per-project budgets — not a blocker for this plan. |
| Duplicate `project_slug` across files not caught until first fetch | Low | High | Cross-file validation at load time in Unit 3 catches duplicates before the first tick. |
| A project file is removed from disk while orchestrator is running | Low | Medium | On hot-reload, a previously-loaded file that's now missing removes its entry from `self.configs`. In-flight dispatches for that project complete; new fetches skip. Log the change loudly. |
| Per-project API keys in different files create auth-key confusion | Low | Low | Each `LinearClient` uses its own project's `resolved_api_key()`. Document the behavior and assume operators intentionally use separate keys. |

## Alternative Approaches Considered

- **Nested `projects:` key in a single workflow.yaml.** Requires a major config schema refactor. Two shapes to validate (nested and flat). Operators would have to migrate. Rejected in favor of the file-per-project approach, which preserves the entire existing `ServiceConfig`.
- **Top-level `orchestrator.yml` for shared globals + per-project files for everything else.** Two config shapes, two parsers, more surface area. The file-per-project + first-file-wins-for-globals approach delivers the same capability with one shape.
- **Per-project concurrency budgets from day one.** Extra state (`_running_by_project`, `max_concurrent_by_project`). Not required for the foundational capability. Deferred.
- **Per-project dashboards on separate ports.** N FastAPI instances. Operators lose the unified view. Rejected — the one-dashboard model is what makes multi-project-in-one-orchestrator meaningfully different from "run N daemons."
- **Polling interval per-project via separate tick tasks.** N independent `_tick` loops. Breaks shared dispatch budget coordination. Rejected in favor of one loop at min(interval) across files.
- **Run N independent orchestrator processes with a dashboard aggregator.** The "simplest" alternative discussed in the parent multi-repo brainstorm. Rejected by user choice here — the multi-file-in-one-process approach is targeted at this exact scope.

## Success Metrics

- **Functional:** A two-project `stokowski workflows/` invocation dispatches tickets from both projects. Each project's issues clone from its own repo(s), render with its own prompts, and complete via its own workflow.
- **Backward compat:** `stokowski workflow.yaml` (single file) shows no observable behavior change vs pre-branch. All 305 existing tests pass unchanged.
- **Isolation:** A broken edit to one project file does not stall any other project's dispatch. Per-project validation errors surface per-project.
- **Tests:** ~20 new integration scenarios pass; full suite at 325+ tests.

## Phased Delivery

### Phase A — Foundation (Units 1-3)

Land as a first commit. Adds the CLI, orchestrator state model, and loading infrastructure. The transitional `self.cfg` property keeps existing tests passing. Dispatch is still single-project behaviorally — Phase B turns on the multi-project machinery.

**Can land independently.** Provides the scaffolding without switching the runtime on. Useful as a boundary for early reviewer feedback.

### Phase B — Runtime dispatch (Units 4-5)

The main switch: per-project fetch loop in `_tick`, unified rejection/eligibility/dispatch over all issues, and the `self.cfg.X` sweep that removes the transitional shim. After this phase, multi-project behavior is live.

Depends on Phase A.

### Phase C — Integration surfaces (Units 6-7)

Startup cleanup, Docker volume/image handling, dashboard snapshot, env var injection. Small surface, low risk.

Depends on Phase B.

### Phase D — Tests + Docs (Units 8-9)

End-to-end multi-project integration tests and all documentation updates. Can run partly in parallel with Phase C.

Depends on Phases A-C for the runtime behavior being exercised.

## Documentation Plan

- `CLAUDE.md`: Multi-project section under Key design decisions; pitfalls update for `_cfg_for_issue` discipline; dict-count bump.
- `README.md`: Multi-project orchestration section under "What Stokowski adds"; link to example.
- New example YAML: two-project stub showing the full shape.
- Inline: every `_cfg_for_issue`, `_primary_cfg`, `_linear_client_for` helper has a clear docstring explaining its role in the multiplexing model.
- After shipping: `docs/solutions/` seed entry on per-issue routing patterns (optional follow-up).

## Operational / Rollout Notes

- No database, no migration, no feature flag. Rolling deploy is safe — a pre-branch orchestrator reading a single-file config and a post-branch orchestrator reading the same file behave identically.
- Caveat for rollback from multi-project to single-project: an operator who's added a second project file and then rolls back to a pre-branch orchestrator will see only the auto-detected `workflow.yaml` processed. Issues from the now-ignored second project stall (their state in Linear is unchanged; they simply aren't polled). The fix is to roll forward again. Not a data-loss scenario.
- Monitor for: (a) duplicate-project_slug startup errors during operator migrations; (b) per-project hot-reload failures appearing in logs (broken file → last-known-good behavior, but operators should see the error); (c) dispatch-budget starvation signals (one project's issues dominate log volume).

## Sources & References

- **Parent plan:** `docs/plans/2026-04-20-001-feat-multi-repo-support-plan.md` — the multi-repo work this plan builds on. Most of the per-issue routing patterns (`_issue_repo`, `_issue_workflow`, cleanup parity, hot-reload snapshot) are reused and extended.
- **Origin brainstorm:** `docs/brainstorms/2026-04-20-multi-repo-support-requirements.md` — the multi-repo brainstorm explicitly flagged multi-project polling as the natural next primitive (one team = one project = one config; the AWS multi-tenant layer is a later evolution).
- **Relevant code:**
  - `stokowski/orchestrator.py` — all ~136 `self.cfg.*` sites; `__init__` / `_cleanup_issue_state`; `_tick` / `_reconcile` / `_handle_gate_responses` / `_handle_retry`; `_run_worker`; `get_state_snapshot`
  - `stokowski/main.py` — `cli()`, `run_orchestrator()`
  - `stokowski/config.py` — `ServiceConfig`, `parse_workflow_file`, `validate_config`
  - `stokowski/linear.py` — `LinearClient` constructor
  - `tests/test_rejection_coldstart.py` — `_StubLinearClient` pattern
  - `tests/test_repos_config.py` — `_make_orch` fixture pattern, parity meta-test
- **Multi-repo pitfalls already applicable here:**
  - `_cleanup_issue_state` parity (CLAUDE.md)
  - ADV-005 hot-reload snapshot (CLAUDE.md + multi-repo plan risk table)
  - Defensive `.get()` on tracking payloads (tracking.py contract)
