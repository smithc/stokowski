---
date: 2026-04-20
topic: multi-repo-support
---

# Multi-Repo Support

## Problem Frame

Stokowski today operates in a 1:1 mode — one Linear project maps to one repo. This works for product-affine teams whose board tracks a single product.

However, in the target operating model some teams use their Linear project as a **team-affine board** that spans multiple repos/products. A single board tracks all the team's work; individual tickets may call for code changes in repo A, repo B, or (occasionally) multiple repos. These teams cannot split their Linear project without breaking their planning workflow, and they cannot run separate Stokowski instances per repo without losing the unified dashboard and central operational model (the AWS MVP commits to one Fargate orchestrator service per platform).

**Frequency claim is unvalidated.** The number of target teams actually blocked by 1:1 coupling today is not yet measured. Before planning begins, each of the 3–5 target teams should be surveyed (repos per team, actively blocked vs. inconvenienced). If <40% are truly blocked, this work should defer behind the AWS hosted-MVP; if 80%+, the premise stands. See the `Resolve Before Planning` section.

**Alternative the v1 design displaces.** A "N Stokowski instances + dashboard aggregator" pattern also satisfies the need for multi-repo visibility on one team. v1 chooses "one orchestrator, N repos" because it extends the AWS one-Fargate-service-per-platform commitment, but the trade is not overwhelming — the simplifying alternative should stay on the table until the team survey is in.

The 1:1 coupling lives in three concrete places in the current codebase:
- `TrackerConfig.project_slug` — a single-valued filter on the Linear GraphQL query (`stokowski/linear.py:15-18`).
- `hooks.after_create` — a single shell string that clones exactly one repo (`workflow.example.yaml`).
- `WorkspaceConfig.root` + flat issue-keyed directories — one root, workspaces keyed only by issue identifier (`stokowski/workspace.py`).

Everything else (orchestrator, prompt assembly, tracking comments, state machine, Docker isolation) is already repo-agnostic. The multi-workflow feature already established `labels as the universal control surface` and supplied the primitives (label-driven routing, triage-as-label-suggester, `_default` synthesis) that multi-repo support naturally layers on top of.

## Requirements

**Repo Routing and Selection**

- R1. **Repos are selected per-ticket via a `repo:<name>` Linear label.** One repo label per ticket in v1. First match wins. Operators or the triage agent apply the label; humans can override by changing the label, same semantics as existing workflow label override.
- R2. **The triage workflow extends to apply `repo:<name>` labels in addition to `workflow:<name>` labels.** Triage remains a label-suggester — it does not route directly. After triage applies labels, the issue re-enters the dispatch cycle and is routed normally. (The triage workflow is defined in `docs/brainstorms/2026-03-24-multi-workflow-requirements.md` as a single-stage workflow that classifies issues and applies `workflow:*` labels; R2 extends its label vocabulary to include `repo:*`.)
- R3. **Unlabeled tickets route to a default repo when one is marked `default: true`.** If no repo is marked default AND the config has multiple repos, unlabeled tickets route to the triage workflow to acquire a `repo:<name>` label. Single-repo configs are trivially defaulted (the sole repo is the default).
- R4. **One and only one repo may be marked `default: true`.** Config validation enforces zero-or-one default. When multiple repos are defined, a `triage` workflow must be present to cover the no-default case.

**Repo Registry Shape (v1)**

- R5. **A `repos:` registry is added to `workflow.yaml`.** v1 scope is the minimum shape needed to route tickets and clone the right codebase. Each entry is identified by a stable name (registry key) and carries only these fields:
  - `name` — the registry key itself (e.g. `api`). Available in templates and prompts as `repo.name`; not a separate field in YAML.
  - `label` — the Linear label used to route tickets to this repo (e.g. `repo:api`).
  - `clone_url` — the URL used by the clone hook.
  - `default` — optional boolean; at most one repo may set this (R4).
  - `docker_image` — optional; the repo's default image for toolchain-bound stages (see R8).

  **Deferred to MVP+ (add when demand surfaces):** per-repo `extra_env` (cross-repo secret-scoping concern — see Resolve Before Planning), per-repo `hooks` override block (homogeneous-stack teams are served by R6's templating; heterogeneous stacks can be added when real cases arrive).
- R6. **Root hooks may be templated with repo metadata, but only when a `repos:` section exists.** When the config defines a `repos:` section, the root `hooks.after_create` (and other hook blocks) are rendered as Jinja2 templates at **dispatch time** (not parse time) with `{{ repo.name }}`, `{{ repo.clone_url }}`, `{{ repo.label }}` available. **Legacy configs with no `repos:` section bypass Jinja rendering entirely** — this preserves R19's "continue to work unchanged" promise for configs whose shell bodies contain literal `{`/`}` characters (e.g. `git config credential.helper '!f() { ...; }; f'`).
- R7. **The v1 registry entry shape is forward-compatible with the MVP+ extraction.** The fields that do land in v1 (`name`, `label`, `clone_url`, `default`, `docker_image`) are the same fields that will live in the platform registry at MVP+ time. Fields deferred to MVP+ (`extra_env`, `hooks`) land in the per-repo `stokowski.yml` when added; they are not retroactively back-ported to v1 `workflow.yaml`. MVP+ migration for fields present in v1 is field-moving, not re-modeling.

**docker_image Resolution**

- R8. **Three-level hybrid resolution for `docker_image`.** Precedence, most-specific first:
  1. Team workflow stage declaration (`StateConfig.docker_image`) — wins when the stage is repo-agnostic and the team wants the same image everywhere.
  2. Repo registry entry default (`repos.<name>.docker_image`) — used for toolchain-bound stages.
  3. Platform / global default (`docker.default_image`) — final fallback.
- R9. **`runner` (claude vs codex) remains a team-workflow-stage-level decision and does not participate in the repo-level hybrid.** Runner is a process decision, not a toolchain decision.

**Single-Repo Cap (v1)**

- R10. **v1 validation rejects tickets with more than one `repo:*` label.** Runtime behavior:
  - The check runs in `_is_eligible`; ineligible tickets are not dispatched.
  - A rejection tracking comment is posted **once per transition into conflict** (deduplicated by scanning existing tracking comments), so polling does not spam the ticket thread.
  - The ticket is marked in an in-memory `_rejected_issues` set; the marker is cleared when a subsequent fetch shows the labels have been edited.
  - **If triage itself produced the conflict** (the triage workflow applied two `repo:*` labels), the dispatcher posts a tracking comment flagging human review rather than looping the ticket back through triage.
  - The cap is a narrow guard line that can be removed when v2 multi-repo scope lands.
- R11. **v1 takes the single-repo cap to defer the v2 execution model; the v2 target is not yet committed.** v2 may be "single agent, N sibling clones," or "parallel lanes (N workers per ticket, synchronized at gates)," or "pipelined per-repo." Each has different strengths on failure scenarios (one PR approved / one rejected, PR linkage failure mid-run, token budget across N codebases, merge-bundle coordination, compound-before-merge race amplification). v1 intentionally closes off none of these by capping at one repo. **v1 is structured to be additive for any v2 model:** workspace key includes `repo_name` (R12), env/prompt vars are scalar but resolvable per-dispatch (R14/R16), state machine stays unchanged (R13). An Alternatives Considered block for v2 should be added to the planning document.

**Workspace and State**

- R12. **v1 workspace key includes the repo name as a suffix: `{workspace.root}/{issue_identifier}-{repo_name}`.** Even in single-repo-per-ticket v1, the repo name is part of the key — so the v1 path is truly a special case of any v2 layout (whether v2 picks sibling clones, parallel lanes, or pipelined-per-repo). For legacy configs with no `repos:` section, the synthetic `_default` repo produces `{workspace.root}/{issue_identifier}-_default`, which preserves the existing path shape closely enough that the workspace directory name changes by one suffix; hooks that hardcode the path should be reviewed during migration. Docker workspace volume naming (`docker_runner.py:workspace_volume_name`) is updated to the same `{prefix}-{issue}-{repo}` shape; the volume-cleanup scan at `docker_runner.py:201-227` must be updated in lock-step.
- R13. **The state machine is unchanged.** One issue, one workflow, one state at a time. The repo is an attribute of dispatch (which clone URL, which image, which hooks), not a new axis of the state machine.

**Prompt Context and Env Vars**

- R14. **The prompt template context gains repo-scoped variables.** Available in Jinja2 prompts: `repo_name`, `repo_clone_url`, `repo_label`. Values are resolved from the repo selected for the current dispatch. **When the synthetic `_default` repo is in use** (legacy 1:1 config, R19), `repo_name` resolves to the literal string `_default` and `repo_clone_url` / `repo_label` resolve to empty strings — prompts that reference these variables should treat empty strings as "repo metadata not available."
- R15. **The auto-generated lifecycle section names the active repo explicitly.** The agent sees which repo it is operating on, mirroring how issue identifier and state are already surfaced. For the synthetic `_default` repo, the lifecycle section omits the repo block rather than showing `_default` and empty-string fields.
- R16. **Agent subprocess env adds `STOKOWSKI_REPO_NAME` and `STOKOWSKI_REPO_CLONE_URL`.** These sit alongside the existing `STOKOWSKI_ISSUE_IDENTIFIER` and are informational only. For the synthetic `_default` repo, `STOKOWSKI_REPO_NAME=_default` and `STOKOWSKI_REPO_CLONE_URL` is unset (not empty string) so hook scripts that test for the variable's presence can distinguish "no repo metadata" from "repo with no URL." Both `agent_env()` and `docker_env()` set these variables consistently (following the existing `STOKOWSKI_ISSUE_IDENTIFIER` pattern).

**Triage Agent**

- R17. **The triage agent is passed the list of available repos.** Mechanism is deferred to planning, but options include: serialized env var (`STOKOWSKI_REPOS_JSON`), a rendered section injected into the triage prompt template, or both. The triage agent uses issue title, description, and linked files/keywords to decide which `repo:<name>` label to apply. **The repo list passed to triage is scoped to the current tenant's repos** — in v1 this is the full `repos:` registry (single tenant), but the API contract is "tenant-scoped list" so MVP+ multi-tenant does not silently widen the label-writing surface.
- R18. **Triage remains a label-suggester, not a router.** After triage applies `workflow:*` and `repo:*` labels, the issue re-enters the dispatch cycle and is routed normally. Humans can override by changing the label before the issue progresses.
- R18a. **Triage repo-label support is a hard prerequisite of v1 ship.** Because R3 routes unlabeled multi-repo tickets into triage expecting it to emit `repo:*` labels, v1 cannot ship with triage that only emits `workflow:*`. The triage prompt update lands in the same release as the `repos:` registry. If R3's no-default path is not exercised (i.e., the team surveyed has only single-repo configs or explicitly marks a default), this prerequisite is moot — but validation still requires triage's presence per R21.
- R18b. **Two-axis partial-correctness is a known failure mode that requires measurement before ship.** Triage applies both `workflow:*` and `repo:*`; compound accuracy matters. Prior to GA, run the triage prompt against a representative sample of historical tickets (target: 50–100) and measure repo-label accuracy. If repo accuracy is below a stated threshold (see `Resolve Before Planning`), either give triage codebase-read access, make repo selection a structured Linear field instead of a label, or require a human-review gate between triage and dispatch.

**Backward Compatibility**

- R19. **Existing 1:1 configs without a `repos:` section continue to work unchanged.** If the config has no `repos:` section but a populated `hooks.after_create`, a synthetic `_default` repo is created at parse time with `name="_default"`, `label=None`, `clone_url=""`, `default=True`. This mirrors the multi-workflow `_default` synthesis pattern (`config.py:620-638`), which created a workflow with `label=None` and `default=True`. The synthetic entry is **explicitly exempt from R21's non-empty `clone_url`/`label` checks**. The root `hooks:` block is passed through to `ensure_workspace()` verbatim — **no Jinja rendering** (R6 explicitly excludes configs with no `repos:` section). Prompt/env variables for the synthetic `_default` follow R14/R15/R16. Operators do not need to migrate.

  **Edge cases a legacy `hooks.after_create` may contain that the synthetic `_default` cannot structurally capture:**
  - Env-var-constructed clone URLs (e.g. `git clone $CLONE_URL_BASE/...`) — works unchanged because hooks are not parsed.
  - Multi-clone hooks (e.g. `git clone frontend && git clone backend && ...`) — works unchanged; Stokowski treats the workspace as pre-seeded and does not rewrite the clone.
  - Non-clone seeding (tarball from S3, pre-baked volume) — works unchanged.

  In all three cases, `repo_clone_url` in prompts/env is empty, which is expected — agents operating on legacy configs have always inferred the codebase from the workspace, not from injected metadata.
- R20. **The Linear GraphQL query shape is unchanged.** `fetch_candidate_issues` still filters by `project_slug`. Repo-label routing happens post-fetch in Python. (Multi-project-per-tenant is flagged as a Deferred to Planning question for AWS MVP+.)

**Validation**

- R21. **Config validation adds the following checks, grouped by concern:**
  - *Repo entry integrity*: each explicitly declared `repos:` entry has a non-empty `clone_url` and `label`; labels are unique across repos. The synthetic `_default` repo (R19) is **exempt** from these checks.
  - *Clone-URL scheme check*: `clone_url` must use `https://`, `ssh://`, or `git@` form. Reject `file://` and reject URLs containing embedded credentials (the `user:pass@host` pattern) — a one-line guard that prevents the most realistic accidental misconfiguration in an internal multi-team setting.
  - *Default constraint*: at most one repo is marked `default: true`. Single-repo configs must have their sole repo marked `default: true` (the "trivially defaulted" case from R3 is enforced, not inferred).
  - *Triage requirement*: if multiple repos are defined and none is marked default, the config must define a `triage` workflow.
  - *Reserved label prefixes*: operator-declared labels on repos (or anywhere in the config) must not collide with the reserved prefixes `workflow:` or `repo:` in unexpected ways. A warning (not error) fires if any operator label is a near-match to a reserved prefix (e.g. `repos:`, `reop:`).
  - *Path safety*: registry names are `[A-Za-z0-9._-]` only (used for path-safe workspace subdirs).

## Success Criteria

- A team whose Linear project spans three repos can dispatch work across all of them from a single Stokowski instance, with tickets correctly routed to the right repo based on `repo:<name>` labels.
- An existing 1:1 config (no `repos:` section) continues to work without any operator migration — including configs whose `hooks.after_create` contains literal `{`/`}` shell syntax.
- The triage agent successfully classifies representative tickets and applies `repo:<name>` labels at or above the accuracy threshold agreed in `Resolve Before Planning`. Humans can override the triage decision by changing the label before execution.
- A ticket with two `repo:*` labels is rejected at dispatch with a clear message; the rejection comment is posted once per transition into conflict and does not spam the thread.
- The v1 `repos:` entry shape is the minimum required for v1 and is forward-compatible with the MVP+ extraction — migration for fields present in v1 moves fields across files; fields deferred to MVP+ land in `stokowski.yml`.
- `docker_image` resolution for the common case (team-stage-declared image OR repo-default image OR platform default) produces the right image per dispatch. **Known gap:** the "team-stage declared AND this specific repo needs a different image for that stage" case is not resolvable under R8 without adding a per-repo stage-override field — see `Resolve Before Planning`.

## Scope Boundaries

- **Not in scope (v2):** multiple repos per single ticket. v1 enforces the single-repo cap via validation. **The v2 execution model is deliberately not committed in v1 requirements** — v1 leaves room for sibling clones, parallel lanes, or pipelined approaches. v1 choices (workspace key, env/prompt scalar vars, unchanged state machine) are structured to be additive for any v2 model.
- **Not in scope (MVP+):** extraction of per-repo fields to a `stokowski.yml` file in each repo. v1 keeps everything inline. The MVP+ step is a config migration, not a new feature.
- **Not in scope (MVP+):** the platform registry layer (team ↔ enabled repos mapping) and the team-workflow-as-its-own-repo pattern. These are part of the AWS multi-tenant platform future and are not v1 work.
- **Not in scope (MVP+):** per-repo `extra_env` and per-repo `hooks` override. The v1 registry omits both. `extra_env` is deferred because its safe introduction requires explicit cross-repo env-scoping semantics that v1 does not provide. Per-repo `hooks` are deferred because R6's root-hook templating covers homogeneous-stack teams; heterogeneous-stack support waits for real demand.
- **Not in scope:** per-repo process overrides (a repo demanding stricter human review, a specific model, a specific permission mode). The doc's team-workflow/repo-card separation places process at team level only. **Decision locked in:** teams with divergent-process-per-repo needs (SOX/PCI/security-infra) fork the workflow label (e.g., `workflow:full-ce` vs `workflow:full-ce-strict`) rather than overriding process in their repo card. Narrow repo-level process fields (`required_permission_mode`, `required_model`) may be added in a future iteration if workflow-label explosion becomes a real problem, but are not v1.
- **Not in scope:** repo-level `stage_overrides` for `docker_image`. R8's 3-level hybrid cannot resolve the "team stage declares an image AND this repo needs a different image for that stage" case. If that case surfaces for the target teams, the resolution model needs to be extended — see `Resolve Before Planning`.
- **Not in scope:** Linear sub-project or team-based repo mapping. The label-driven approach does not depend on teams restructuring their Linear hierarchy.
- **Not in scope:** Linear custom fields (instead of labels) for repo selection. Labels are reused for consistency with the existing `workflow:*` convention; Linear custom fields as a structured alternative are flagged under `Resolve Before Planning`.
- **Not in scope:** merge-bundle PR coordination. v1 is single-repo, so each ticket produces at most one PR — existing merge stage logic suffices.
- **Not in scope:** rework-targeting per repo. Deferred with multi-repo scope (v2).

## Key Decisions

- **Labels are the control surface for repo routing, matching the existing workflow-label pattern.** Workflow label and repo label are orthogonal axes: both can be pre-applied by humans, applied by triage, or changed mid-flight. **Known cost at N=3+ axes:** adding `repo:*` on top of `workflow:*` (and any future platform-tenant label) compounds the cognitive load on ticket authors and the typo/collision surface in Linear's shared label namespace. This spec accepts that cost; R21 mitigates with reserved-prefix validation and near-match warnings. The Linear-custom-field alternative is logged under `Resolve Before Planning` as a credible alternate surface if label-discipline proves too expensive in practice.
- **Team-workflow / repo-card separation of concerns.** Team `workflow.yaml` owns process (states, gates, transitions, model defaults, prompts, permissions). Repo metadata (inline in v1, `stokowski.yml` at MVP+) owns codebase interaction (clone, docker_image, MVP+ hooks/env). These are distinct concerns that don't impose on each other — multi-repo tickets union the codebase metadata and run the team workflow once.
- **Workflow-label forking is the accepted escape valve for repo-level process divergence.** Repos that legitimately need stricter or different process (regulated-infra, SOX/PCI, security-sensitive) handle this by using a different workflow label on their tickets (e.g., `workflow:full-ce-strict` vs `workflow:full-ce`), not by overriding process in their repo card. Known cost: teams with many (repo × compliance-regime) combinations see workflow-label proliferation. Narrow repo-level process fields (`required_permission_mode`, `required_model`) are **deferred** — not rejected — and may be added if workflow-label explosion becomes untenable in practice.
- **v1 registry entry is the minimum shape needed for v1.** `{name, label, clone_url, default, docker_image}`. Forward-compatible with MVP+ extraction but not over-shaped for it. Fields deferred to MVP+ (`extra_env`, per-repo `hooks`) land when real demand surfaces — addition at MVP+ is itself a field-moving operation. Rejected alternatives: "inline all future-state fields now" (speculative complexity: adds code paths, validation, documentation surface for use cases with no v1 success criterion); "inline minimum and template everything from root" (R6 already does this for the common homogeneous-stack case; heterogeneous stacks wait).
- **3-level hybrid `docker_image` resolution for the common case.** Stage-declared > repo-default > platform-default. Rejected: stage-only (fails on heterogeneous stacks); repo-only (fails when team wants to fix an image for a repo-agnostic stage). **Known gap:** the (team-stage-declared, this-repo-needs-a-different-image-for-that-stage) case is not resolvable — e.g., a generic implementer image added to a team workflow can't be overridden for a Rust repo that needs `rustc`. Escape valves: fork the stage (defeats "stages are atoms"), fork the workflow label, or set the team-stage image to null (collapses to 2-level repo+platform). If target teams hit this case, a per-repo `stage_overrides` field is the narrow addition — flagged under `Resolve Before Planning`.
- **Ship single-repo-only in v1; do not commit a v2 execution model yet.** v1's job is to route by label and clone the right repo. v2's question ("how does one agent or N agents handle one ticket spanning multiple repos?") has at least three credible answers (sibling clones, parallel lanes, pipelined) with different strengths. v1 defers that question by capping at one repo, and structures its data shape so any v2 model can build on it without rework.
- **Runner stays team-workflow-stage-level.** Process concern (different agent perspective), not toolchain concern. Repos don't override.

## Visual Aids

**Config architecture: v1 vs MVP+ shape**

```
v1 — everything inline in one workflow.yaml                MVP+ — three-way split
──────────────────────────────────────────                 ──────────────────────────────────────
                                                           Platform registry (per-tenant/team)
workflow.yaml                                                ├─ team-id, workflow_repo URL
  ├─ tracker: { project_slug: ... }                          ├─ enabled_repos:
  ├─ workflows: { ... process ... }                          │    ├─ { name, label, clone_url,
  ├─ states:    { ... stages ... }                           │    │   docker_image (repo default) }
  ├─ repos:                                                  │    └─ ...
  │    ├─ api:                                               
  │    │    { name, label, clone_url,                       Team workflow repo (Git)
  │    │      default, docker_image }                          └─ workflow.yaml
  │    └─ ...                                                     ├─ workflows (process)
  └─ hooks:  (templated over repo                                 └─ states (process)
              metadata only when                             
              repos: section exists)                        Per-repo stokowski.yml (in each repo)
                                                             ├─ hooks          ← added at MVP+
                                                             ├─ extra_env      ← added at MVP+
                                                             └─ docker_image (optional override)
```

**Dispatch flow for an unlabeled ticket (multi-repo config)**

```
Linear issue (no workflow:* or repo:* labels)
  │
  ▼
Stokowski fetch (project_slug filter, unchanged GraphQL)
  │
  ▼
Workflow resolution: no label match, no default workflow ──▶ triage workflow
  │
  ▼
Triage agent
  ├─ reads issue title, description, keywords
  ├─ applies workflow:<X> label
  └─ applies repo:<Y> label
  │
  ▼
Terminal state for triage → issue moves to "Todo" (recycle)
  │
  ▼
Next tick: Stokowski re-fetches, now sees labels
  │
  ▼
Workflow resolved: X. Repo resolved: Y.
  │
  ▼
Workspace = {root}/{issue_identifier}-{repo_name}
  ├─ clone using repos[Y].clone_url
  ├─ root hooks rendered with repo.Y metadata (Jinja dispatch-time,
  │   because `repos:` section exists in this config)
  ├─ container image = hybrid resolve(stage, Y)
  └─ agent runs state machine
```

**docker_image resolution precedence**

```
Stage s, Repo r → image?
  │
  ▼
1. states[s].docker_image set?               ──▶ use it
  │  (team workflow stage declaration)
  │
  ▼ no
2. repos[r].docker_image set?                ──▶ use it
  │  (repo registry default)
  │
  ▼ no
3. docker.default_image set?                 ──▶ use it
  │  (platform / global default)
  │
  ▼ no
  ERROR (caught at validation time)
```

## Dependencies / Assumptions

- **Assumption (not validated by survey):** enough of the 3–5 target teams are blocked by 1:1 coupling today to justify v1 now. This brainstorm proceeds without a team survey; if planning surfaces evidence that the premise doesn't hold (e.g. all target teams are single-repo), the work may be re-scoped or deferred behind the AWS lift.
- **Assumption (requires pre-GA validation):** the triage agent can reliably classify the target repo from issue title + description alone. The accuracy bar compounds across two label axes (workflow + repo). Planning should include a pre-GA accuracy-measurement step (50–100 historical tickets, ≥90% repo-label accuracy target). Below the bar, the fallback is one of: give triage codebase-read access, switch repo selection to a structured Linear field, or add a human-approval gate between triage and dispatch.
- **Decision:** multi-repo ships in parallel with the AWS Fargate lift (not sequenced after it). Planning takes responsibility for sequencing any conflicts (e.g., v2's workspace layout vs S3 Files constraints).
- Linear supports enough custom labels for combined workflow + repo routing. No known limit concern.
- Teams adopt a labeling convention for repo selection (`repo:<name>`), analogous to the existing `workflow:<name>` convention.
- The compound-before-merge ordering fix (from the 2026-03-28 brainstorm) continues to mitigate the Linear GitHub auto-close race. v2 multi-repo will amplify this concern and may require further changes, but v1 single-repo inherits the existing mitigation unchanged.
- The AWS multi-tenant commitments (2026-04-14 brainstorm) remain load-bearing: one orchestrator service per platform, tenants as an outer layer, `tenants.yaml` as the registry. The future-state repo architecture (platform registry + team workflow repo + per-repo `stokowski.yml`) is an evolution of that tenant model, not a replacement.
- **Platform-default workflows** ("default platform behavior" when a team onboards without its own workflow repo) are flagged as a future-state concept in the AWS multi-tenant brainstorm, not a v1 requirement in this document. Naming, versioning, and catalog shape are deferred to that context.

## Outstanding Questions

### Deferred to Planning

Technical and validation questions that planning or pre-GA implementation will answer; not blockers for brainstorm completion. The prior "Resolve Before Planning" questions have been converted to assumptions (see `Dependencies / Assumptions`), locked-in decisions (see `Key Decisions` and `Scope Boundaries`), or deferred items below.

- [Affects R5, R6][Technical] Exact Jinja2 variable shape for repo metadata in root hooks. Flat (`{{ repo_clone_url }}`) or nested (`{{ repo.clone_url }}`)? Nested is proposed throughout this document for readability, and the choice should be consistent across R14's prompt context and R6's hook context to avoid silent `_SilentUndefined` failures.
- [Affects R17][Technical] Mechanism for passing the repo list to the triage agent prompt: env var serialization (`STOKOWSKI_REPOS_JSON`), a rendered section injected into the triage prompt template, or both. Commit to a concrete variable name at planning time.
- [Affects R3, R1][Technical] Resolution method placement. `ServiceConfig.resolve_repo(issue) -> RepoConfig` mirroring `resolve_workflow` (`config.py:310-326`); per-issue `_issue_repo` cache added to `Orchestrator.__init__` with a matching line in `_cleanup_issue_state` (the 11-entry cleanup dict noted in CLAUDE.md); call site likely inside `_dispatch`. Hot-reload must handle repos removed mid-dispatch, mirroring `_get_issue_workflow_config`'s resilience.
- [Affects R10][Technical] Runtime specifics for the single-repo-cap rejection: wire into `_is_eligible`, confirm comment-dedup approach against existing tracking-comment parsing (`tracking.py`), and decide whether triage-produced conflicts warrant an extra escalation path beyond the generic tracking comment.
- [Affects R12][Needs research] Docker workspace-volume naming needs to update to `{prefix}-{issue}-{repo}` consistently with R12's workspace-key change. Confirm `docker_runner.py:workspace_volume_name` and the cleanup scan at `:201-227` are updated in lock-step, and that the volume-key rename does not break in-flight volumes at the v1 ship moment (expected: no in-flight state survives a deployment, but worth verifying).
- [Affects R2, R17][Needs research] Whether the triage agent needs repo codebase access for classification, or whether issue metadata alone is sufficient. Answer drives the threshold decision in `Resolve Before Planning`.
- [Affects R6, R19][Technical] `_SilentUndefined` semantics for hook templating: the prompt pipeline uses silent-undefined; the hook pipeline's choice should be stated. Silent-undefined in hooks will turn a missing repo-metadata variable into an empty string silently — potentially dangerous in `git clone $EMPTY` contexts. Prefer `StrictUndefined` for hooks so misconfigurations fail loud.
- [Affects R8, R9][Technical] Whether existing deployments relying on `docker.default_image` get the new hybrid resolution silently applied (backward-compatible, no repo-level image declared → same as old behavior) or opt-in. Default: silently applied; confirm during planning.
- [Affects R8][Technical] The startup Docker image pre-pull loop (`orchestrator.py:234-241`) currently iterates `docker.default_image` plus every `StateConfig.docker_image`. Under R8's 3-level hybrid, the pre-pull set must be extended to include every `repos.<name>.docker_image` so first-dispatch doesn't trigger a cold `docker pull` during an agent turn.
- [Affects R16, docker_env][Technical] `STOKOWSKI_REPO_NAME` and `STOKOWSKI_REPO_CLONE_URL` are set after env construction in both `agent_env()` and `docker_env()`, mirroring `STOKOWSKI_ISSUE_IDENTIFIER`. When per-repo `extra_env` lands (MVP+), its vars are unioned with `docker.extra_env` allowlist rather than bypassing it — so the Docker isolation contract is explicitly extended, not weakened.
- [Affects R20, AWS multi-tenant][Needs research] Whether a tenant under AWS multi-tenancy has one `project_slug` or N. If N, the fetch path grows tenant-scoped multi-project support; that interacts with repo routing in ways not covered here. Low urgency for v1 (single tenant / one project), load-bearing for MVP+.
- [Affects R3, R12][Technical] Repo-label change mid-flight: R12 (multi-workflow) covers workflow-label changes mid-flight but not repo-label changes. When a human changes `repo:api` → `repo:web` after the workspace is already cloned, does the workspace rebuild? Does the agent session restart? Expected MVP behavior: treat repo-label change as workflow-restart (rework_to entry_state of the current workflow). Confirm during planning.
- [Affects R8][Technical] **Per-repo `stage_overrides` for docker_image.** The R8 3-level hybrid cannot resolve the (team-stage-declared image, repo-specific toolchain need) case. Assumed rare among target teams; address if it surfaces via a narrow `repos.<name>.stage_overrides: { implement: image-name }` field.
- [Affects R1, R2][Needs research] **Linear-custom-field alternative to `repo:*` labels.** For the captive internal audience there is design authority to use Linear custom fields (typed, dropdown-constrained) as the repo-selection surface — more discoverable, less typo-prone, but a different UX than `workflow:*` labels. Labels are the v1 choice; the custom-field alternative should be evaluated during planning if labels prove brittle in pilot use.

## Next Steps

→ `/ce:plan` for structured implementation planning.
