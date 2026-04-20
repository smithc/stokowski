"""Main orchestration loop - polls Linear, dispatches agents, manages state."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jinja2 import (
    Environment,
    StrictUndefined,
    TemplateSyntaxError,
    UndefinedError,
)

from .config import (
    ClaudeConfig,
    HooksConfig,
    RepoConfig,
    ServiceConfig,
    StateConfig,
    ParsedConfig,
    WorkflowConfig,
    _resolve_linear_state_name,
    merge_state_config,
    parse_workflow_file,
    validate_config,
)
from .docker_runner import (
    check_docker_available,
    cleanup_orphaned_containers,
    cleanup_orphaned_volumes,
    kill_container,
    pull_image,
)
from .linear import LinearClient
from .models import Issue, RetryEntry, RunAttempt
from .prompt import (
    assemble_prompt,
    build_lifecycle_section,
    render_hook_template,
    render_hooks_for_dispatch,
)
from .runner import run_agent_turn, run_turn
from .tracking import (
    has_pending_rejection,
    make_bounded_drop_comment,
    make_cancel_comment,
    make_cancel_reference_comment,
    make_fired_comment,
    make_gate_comment,
    make_migrated_comment,
    make_rejection_comment,
    make_schedule_error_comment,
    make_state_comment,
    parse_fired_by_slot,
    parse_latest_schedule_error,
    parse_latest_tracking,
)
from .workspace import ensure_workspace, remove_workspace, sanitize_key
from . import scheduler
from ._fire_helpers import (
    MAX_FIRE_ATTEMPTS,
    build_child_description,
    build_child_title,
    decide_materialize_step,
    find_existing_child_for_slot,
    max_seq_from_parsed,
    slot_label_name,
    watermarks_from_parsed,
    workflow_label_name,
)

logger = logging.getLogger("stokowski")


def _resolve_docker_image(
    state_cfg: StateConfig | None,
    repo: RepoConfig,
    platform_default: str,
) -> str:
    """3-level Docker image resolution (R8).

    Precedence, most-specific first:
    1. ``state_cfg.docker_image`` — team workflow stage declaration;
       wins when the stage is repo-agnostic and the team wants the same
       image everywhere.
    2. ``repo.docker_image`` — repo registry entry default; used for
       toolchain-bound stages on heterogeneous teams.
    3. ``platform_default`` — the configured ``docker.default_image``
       fallback.

    Returns an empty string when no image is configured at any level;
    the caller decides whether that's acceptable.
    """
    if state_cfg and state_cfg.docker_image:
        return state_cfg.docker_image
    if repo.docker_image:
        return repo.docker_image
    return platform_default or ""


def _render_hooks_best_effort(
    hooks: HooksConfig,
    repo: RepoConfig,
    synthesized: bool,
) -> HooksConfig:
    """Render hooks for teardown paths without aborting on UndefinedError.

    Used from remove_workspace call sites where a mid-flight config typo
    shouldn't prevent cleanup. The hook execution would fail anyway if the
    typo is real, but we prefer a clean cleanup path over re-raising here.
    """
    try:
        return render_hooks_for_dispatch(hooks, repo, synthesized)
    except UndefinedError as e:
        logger.warning(
            "Hook rendering failed during teardown for repo=%s: %s — "
            "passing raw hooks through (execution may fail)",
            repo.name, e,
        )
        return hooks


# R21: Number of consecutive reconcile ticks a template must be absent from
# Linear before we treat it as hard-deleted and cascade-cleanup children +
# persistent workspace. Transient API errors / single-tick blips reset the
# counter to 0, so this guards against false positives.
TEMPLATE_HARD_DELETE_THRESHOLD_TICKS = 3


# R13: Maximum number of archive mutations issued per retention sweep tick.
# Bounds the per-tick work so a large backlog can't block the orchestrator
# loop. Oldest-first ordering + next-tick continuation handles overflow.
# TODO: expose via ``ServerConfig.retention_budget_per_tick`` if operators
# hit the backlog in production.
RETENTION_BUDGET_PER_TICK = 20

# R13: Consecutive archive failures on the same child before the sweep
# gives up on that child (poison pill). Prevents an un-archivable issue
# from wasting one budget slot every tick.
RETENTION_POISON_PILL_THRESHOLD = 5


def select_retention_candidates(
    children: list[Issue],
    now: datetime,
    retention_days: int,
    terminal_type_values: tuple[str, ...] = ("completed", "canceled"),
) -> list[Issue]:
    """Pure helper: pick archive candidates from a template's children.

    Filters to terminal, non-archived children whose ``updated_at`` age
    exceeds ``retention_days``. Returns oldest-first (ascending
    ``updated_at``), so callers can slice from the front to honour the
    per-tick budget.

    Mirrors the style of ``cleanup_old_logs`` / ``enforce_size_limit``
    below — pure, unit-testable, no I/O.
    """
    if retention_days < 1:
        return []
    cutoff = now - timedelta(days=retention_days)
    eligible: list[Issue] = []
    for child in children:
        if child is None or not child.id:
            continue
        if child.archived_at is not None:
            continue
        state_type = (child.state_type or "").lower()
        if state_type not in terminal_type_values:
            continue
        updated = child.updated_at
        if updated is None:
            # No timestamp = can't reason about age, skip conservatively.
            continue
        if updated >= cutoff:
            continue
        eligible.append(child)
    eligible.sort(key=lambda c: c.updated_at or datetime.min.replace(tzinfo=timezone.utc))
    return eligible


def classify_missing_id(
    id_: str,
    templates: set[str],
    running: set[str] | dict[str, Any],
    pending_gates: set[str] | dict[str, Any],
) -> str:
    """Classify a Linear-absent issue id for reconciliation branching.

    Returns one of:
      * ``"template"`` — id is tracked in ``templates`` (apply N-tick threshold)
      * ``"gated"``    — id is gated but not running (immediate cleanup)
      * ``"running"``  — id has a running worker (immediate cleanup)
      * ``"unknown"``  — id is in none of the tracking sets

    Membership checks are O(1) against plain sets or dict key-views. The
    helper is deliberately pure so it can be unit-tested without
    constructing an Orchestrator. When an id lives in more than one bucket
    (e.g. a template id somehow also in ``running``), ``"template"`` wins
    so the cascade path is chosen consistently.
    """
    if id_ in templates:
        return "template"
    if id_ in running:
        return "running"
    if id_ in pending_gates:
        return "gated"
    return "unknown"


class Orchestrator:
    def __init__(self, workflow_paths):
        """
        Accepts either:
          - A single path (``str`` or ``Path``) — legacy single-project mode.
          - A sequence of paths — multi-project mode. Order determines the
            "primary" project (first entry) whose global settings apply.

        The constructor only declares state. ``self.configs`` is populated by
        ``_load_all_workflows()`` — see ``start()`` and ``_tick()``.
        """
        if isinstance(workflow_paths, (str, Path)):
            self.workflow_paths: list[Path] = [Path(workflow_paths)]
        else:
            self.workflow_paths = [Path(p) for p in workflow_paths]
        if not self.workflow_paths:
            raise ValueError("Orchestrator requires at least one workflow path")

        # Back-compat: keep ``workflow_path`` pointing at the primary file so
        # any lingering single-file references keep working during the Unit 5
        # sweep. Removed at end of Unit 5.
        self.workflow_path: Path = self.workflow_paths[0]

        # Multi-project state (Unit 2): project_slug -> ParsedConfig.
        # Insertion order matters — the first loaded file is the "primary"
        # whose shared globals (max_concurrent_agents, server.port) apply.
        self.configs: dict[str, ParsedConfig] = {}
        # Parallel map: project_slug -> source Path (for workflow_dir resolution).
        self._config_paths: dict[str, Path] = {}
        self._linear_clients: dict[str, LinearClient] = {}

        # Cached min polling interval across all configs (ms). Set by
        # _load_all_workflows(). Read by start() poll loop.
        self._polling_interval_ms: int = 0

        # Transitional back-compat shim — returns the first loaded
        # ParsedConfig. Unit 5 removes the ``self.cfg`` property that reads
        # from this. Kept so the existing (mostly single-project) test suite
        # continues to pass during the refactor.
        self.workflow: ParsedConfig | None = None

        # Runtime state
        self.running: dict[str, RunAttempt] = {}  # issue_id -> RunAttempt
        self.claimed: set[str] = set()
        self.retry_attempts: dict[str, RetryEntry] = {}
        self.completed: set[str] = set()

        # Aggregate metrics
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_tokens: int = 0
        self.total_seconds_running: float = 0

        # Internal
        self._linear: LinearClient | None = None  # legacy single-client slot (Unit 5 removes)
        self._tasks: dict[str, asyncio.Task] = {}
        self._retry_timers: dict[str, asyncio.TimerHandle] = {}
        self._child_pids: set[int] = set()  # Track claude subprocess PIDs
        self._last_session_ids: dict[str, str] = {}  # issue_id -> last known session_id
        self._jinja = Environment(undefined=StrictUndefined)
        self._running = False
        self._last_issues: dict[str, Issue] = {}
        self._last_completed_at: dict[str, datetime] = {}  # issue_id -> last worker completion time

        # State machine tracking
        self._issue_current_state: dict[str, str] = {}   # issue_id -> internal state name
        self._issue_state_runs: dict[str, int] = {}       # issue_id -> run number for current state
        self._pending_gates: dict[str, str] = {}           # issue_id -> gate state name
        self._issue_workflow: dict[str, str] = {}          # issue_id -> workflow name
        self._issue_repo: dict[str, str] = {}              # issue_id -> repo name (multi-repo)
        self._issue_project: dict[str, str] = {}           # issue_id -> project_slug (multi-project)

        # Cancellation tracking
        self._force_cancelled: set[str] = set()  # issue_ids cancelled by reconciliation
        self._background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks

        # R10 single-repo cap — the async rejection pre-pass populates this
        # set before the synchronous eligibility loop runs; _is_eligible
        # only observes membership (no I/O in the hot path). Label-change
        # invalidation clears entries so dispatch resumes when operators fix
        # the labels. Orchestrator restart loses the set, but the rejection
        # pre-pass repopulates it on the first tick by scanning tracking
        # comments via ``has_pending_rejection``.
        self._rejected_issues: set[str] = set()

        # Cold-start migration marker — tracks issue IDs for which we have
        # posted a one-time migrated-comment during this orchestrator run,
        # so a sequence of gate fetches on the same issue does not spam.
        self._migrated_issues: set[str] = set()

        # Config-error block set — populated when a dispatch fails due to a
        # permanent config problem (hook template typo, Jinja2 syntax error).
        # _is_eligible observes this to prevent retry loops on unrecoverable
        # errors. Cleared on every successful _load_workflow so dispatch
        # resumes when the operator fixes workflow.yaml. Also cleared on
        # issue-labels change (_last_issues diff) in case the issue was
        # moved to a different repo/workflow whose config is healthy.
        self._config_blocked: set[str] = set()

        # Rejection fetch-failure marker — when _process_rejections can't
        # fetch comments for a dual-label issue, we fail closed (add to
        # _rejected_issues) to prevent dispatching with arbitrary first-wins
        # repo routing. This companion set tracks "pessimistic" rejections
        # that should be re-evaluated on the next tick instead of sticking
        # until labels change. See ADV-003.
        self._rejection_fetch_pending: set[str] = set()

        # Prior-tick labels snapshot — captured BEFORE _last_issues is
        # updated on each tick. Consumed by _process_rejections to detect
        # label changes since the previous tick (see COR-001). Keyed by
        # issue id, holds the sorted lowercase label list from the prior
        # tick's Issue. Empty on the first tick / orchestrator restart
        # (which is correct: no prior tick to compare against).
        self._prev_issue_labels: dict[str, list[str]] = {}

        # Per-template state (scheduled-jobs feature).
        # Any NEW per-template dict added here MUST be mirrored in
        # _cleanup_template_state() below (see CLAUDE.md learning #1).
        self._templates: set[str] = set()                                 # template Linear issue IDs
        self._template_snapshots: dict[str, Issue] = {}                   # template_id -> Issue (latest fetch)
        self._template_children: dict[str, set[str]] = {}                 # template_id -> set of active child issue IDs
        self._child_to_template: dict[str, str] = {}                      # child_id -> template_id (reverse index)
        self._template_last_fired: dict[str, datetime] = {}               # template_id -> last successful fire timestamp
        self._template_last_seen: dict[str, int] = {}                     # template_id -> N-tick-absent counter (Unit 8)
        self._template_error_since: dict[str, datetime] = {}              # template_id -> when moved to Error state
        self._template_watermark_seq: dict[str, int] = {}                 # template_id -> next seq counter value
        self._template_fire_attempts: dict[tuple[str, str], int] = {}     # (template_id, slot) -> attempt counter
        self._template_next_fire_at: dict[str, datetime] = {}             # template_id -> computed next-fire time (dashboard, Unit 13)
        self._template_seq_seeded: set[str] = set()                       # template_ids whose seq counter was rehydrated from Linear this process lifetime

        # Retention sweep state (Unit 10, R13). Not keyed by template —
        # by child id — so these survive template-level cleanup and
        # re-check on subsequent ticks.
        self._retention_poison_pill_counts: dict[str, int] = {}           # child_id -> consecutive archive failure count
        self._retention_backlog_detected: bool = False                    # set True when a sweep fills its budget (dashboard, Unit 13)
        self._retention_last_archive_at: dict[str, datetime] = {}         # child_id -> last successful archive wall-clock (optional, for dashboard)

    @property
    def cfg(self) -> ServiceConfig:
        """Transitional shim: returns the primary (first-loaded) project cfg.

        Kept so the existing (mostly single-project) call sites and tests
        continue to work during the Unit 5 sweep. Post-dispatch sites should
        migrate to ``self._cfg_for_issue(issue_id)``; pre-dispatch shared-
        globals sites should migrate to ``self._primary_cfg()`` explicitly.
        Removed at the end of Unit 5.
        """
        if self.workflow is None and self.configs:
            self.workflow = next(iter(self.configs.values()))
        assert self.workflow is not None, "cfg accessed before _load_all_workflows()"
        return self.workflow.config

    def _primary_cfg(self) -> ServiceConfig:
        """Return the first-loaded project's config (for shared globals).

        Shared globals (``agent.max_concurrent_agents``, ``server.port``) come
        from whichever project file loaded first, case-insensitively sorted.
        ``polling.interval_ms`` is NOT read from here — it's cached on
        ``self._polling_interval_ms`` as the min across all files.
        """
        if not self.configs:
            raise RuntimeError("_primary_cfg() called before _load_all_workflows()")
        return next(iter(self.configs.values())).config

    def _cfg_for_issue(self, issue_id: str) -> ServiceConfig:
        """Return the ServiceConfig for the project that owns ``issue_id``.

        Issue→project binding is established at fetch time in ``_tick`` /
        ``_reconcile`` / ``_handle_gate_responses``. If this raises, the
        caller either forgot to bind the issue first (a bug) or is operating
        on a stale issue_id whose project mapping was cleaned up.
        """
        slug = self._issue_project.get(issue_id)
        if slug is None:
            raise RuntimeError(
                f"No project binding for issue_id={issue_id} — "
                "caller must stamp _issue_project before calling _cfg_for_issue"
            )
        parsed = self.configs.get(slug)
        if parsed is None:
            raise RuntimeError(
                f"issue_id={issue_id} bound to unknown project_slug={slug!r}"
            )
        return parsed.config

    def _workflow_dir_for_issue(self, issue_id: str) -> Path:
        """Parent directory of the project file owning ``issue_id``.

        Used for prompt-file path resolution — project-B prompts must resolve
        against project-B's directory, not project-A's.
        """
        slug = self._issue_project.get(issue_id)
        if slug is not None and slug in self._config_paths:
            return self._config_paths[slug].parent
        return self.workflow_path.parent

    def _linear_client_for(self, project_slug: str) -> LinearClient:
        """Lazy per-project LinearClient construction."""
        client = self._linear_clients.get(project_slug)
        if client is not None:
            return client
        parsed = self.configs.get(project_slug)
        if parsed is None:
            raise RuntimeError(
                f"No config loaded for project_slug={project_slug!r}"
            )
        client = LinearClient(
            endpoint=parsed.config.tracker.endpoint,
            api_key=parsed.config.resolved_api_key(),
        )
        self._linear_clients[project_slug] = client
        return client

    async def _evict_project(self, project_slug: str) -> None:
        """Drop a project from self.configs and close its LinearClient.

        Called when a previously-loaded project file disappears from disk AND
        no running/retrying/gated issues remain for that slug. In-flight
        workers hold their pinned cfg snapshot and are unaffected.
        """
        client = self._linear_clients.pop(project_slug, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
        self.configs.pop(project_slug, None)

    def _load_workflow(self) -> list[str]:
        """Backward-compat wrapper around _load_all_workflows.

        Returns a flat list of error strings (prefixed by file path for
        non-single-project configs) suitable for existing callers.
        Unit 3 replaces most call sites with _load_all_workflows directly.
        """
        errors_by_slug = self._load_all_workflows()
        # Map errors-by-project back to a flat list preserving order.
        flat: list[str] = []
        for key, errs in errors_by_slug.items():
            if not errs:
                continue
            for e in errs:
                flat.append(f"{key}: {e}" if key else e)
        return flat

    def _load_all_workflows(self) -> dict[str, list[str]]:
        """Load/reload every configured workflow file independently.

        Returns a dict keyed by ``project_slug`` (or by the file path when
        parse fails before a slug is known) whose values are lists of
        error strings. Empty list means that project loaded clean.

        Per-file isolation: if one file fails mid-run, its previous cached
        ParsedConfig stays in self.configs and running agents keep working.
        First-load failure propagates to caller via the error map.
        """
        errors_map: dict[str, list[str]] = {}
        new_configs: dict[str, ParsedConfig] = {}
        new_paths: dict[str, Path] = {}
        # Resolve each configured file path, collecting error entries by slug
        # when available and by path otherwise.
        current_path_set: set[Path] = set(self.workflow_paths)

        for path in self.workflow_paths:
            path_key = str(path)
            try:
                parsed = parse_workflow_file(path)
            except Exception as e:
                errors_map[path_key] = [f"Workflow load error: {e}"]
                continue

            slug = parsed.config.tracker.project_slug or path_key

            errs = validate_config(parsed.config)

            # Cross-file duplicate slug check — compare against slugs that
            # have already loaded THIS tick (not against historical ones).
            if slug in new_paths:
                errs.append(
                    f"duplicate project_slug={slug!r} — also declared in "
                    f"{new_paths[slug]}"
                )

            if errs:
                errors_map[slug] = errs
                continue

            new_configs[slug] = parsed
            new_paths[slug] = path

        # Merge policy:
        #   - For every slug in new_configs → replace in self.configs.
        #   - For every slug in self.configs whose file is still present in
        #     self.workflow_paths but failed to parse → keep last-known-good.
        #   - For every slug in self.configs whose file is no longer in
        #     self.workflow_paths AND with no in-flight work → evict.
        has_inflight = (
            set(self.running.keys())
            | set(self.retry_attempts.keys())
            | set(self._pending_gates.keys())
        )
        active_slugs = {
            self._issue_project[iid]
            for iid in has_inflight
            if iid in self._issue_project
        }

        # Replace entries that loaded cleanly this tick.
        for slug, parsed in new_configs.items():
            self.configs[slug] = parsed
            self._config_paths[slug] = new_paths[slug]

        # Evict only slugs whose source file is no longer on disk (not in
        # self.workflow_paths) AND without in-flight work. This preserves
        # last-known-good for files present-but-broken.
        stale_slugs: list[str] = [
            slug
            for slug, path in list(self._config_paths.items())
            if path not in current_path_set and slug not in active_slugs
        ]
        for slug in stale_slugs:
            client = self._linear_clients.pop(slug, None)
            if client is not None:
                self._fire_and_forget(client.close())
            self.configs.pop(slug, None)
            self._config_paths.pop(slug, None)

        # Update primary reference for the transitional self.cfg shim.
        if self.configs:
            self.workflow = next(iter(self.configs.values()))

        # Cache min polling interval across all healthy configs.
        if self.configs:
            self._polling_interval_ms = min(
                parsed.config.polling.interval_ms
                for parsed in self.configs.values()
            )

        # Clear _config_blocked on globally healthy load, mirroring legacy
        # behavior (operator fix resumes all blocked issues).
        if not errors_map:
            self._config_blocked.clear()

        return errors_map

    def _ensure_linear_client(self) -> LinearClient:
        """Legacy single-client accessor.

        Returns the primary project's client. Unit 5 sweep migrates callers
        to ``_linear_client_for(project_slug)`` with an explicit slug. Kept
        during Unit 2-4 so existing call sites in the orchestrator keep
        working before the sweep.

        Test stub support: if ``self._linear`` is set explicitly (as
        existing tests do), it takes priority — this lets tests continue to
        stub a single client against the primary project.
        """
        if self._linear is not None:
            return self._linear
        if not self.configs:
            self._linear = LinearClient(
                endpoint=self.cfg.tracker.endpoint,
                api_key=self.cfg.resolved_api_key(),
            )
            return self._linear
        primary_slug = next(iter(self.configs.keys()))
        return self._linear_client_for(primary_slug)

    # -- Kill / cleanup helpers --

    @staticmethod
    def _kill_pid(pid: int) -> None:
        """Kill a process by PID. Send SIGKILL to process group, fall back to individual kill.

        os.killpg(SIGKILL) is atomic for the process group. Claude Code runs
        with start_new_session=True (its own pgrp), so this kills the agent
        and any direct children.
        """
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    async def _kill_worker(self, issue_id: str, reason: str) -> None:
        """Kill a running worker: subprocess, container, then async task.

        Order matters: the subprocess must be killed BEFORE the task is
        cancelled, because CancelledError does not propagate to child processes.
        Handles missing RunAttempt gracefully (e.g., for gated issues).
        """
        attempt = self.running.get(issue_id)

        # 1. Kill subprocess PID (must happen first)
        if attempt and attempt.pid:
            self._kill_pid(attempt.pid)
            self._child_pids.discard(attempt.pid)

        # 2. Kill Docker container
        if attempt and attempt.container_name:
            try:
                await kill_container(attempt.container_name)
            except Exception as exc:
                logger.warning(
                    f"Failed to kill container {attempt.container_name}: {exc}"
                )

        # 3. Cancel async task last
        task = self._tasks.get(issue_id)
        if task and not task.done():
            task.cancel()

        logger.info(f"Killed worker issue={issue_id} reason={reason}")

    def _cleanup_issue_state(self, issue_id: str) -> None:
        """Remove all per-issue tracking state. Idempotent — safe to call multiple times.

        Any new per-issue state dict added to Orchestrator.__init__ must also
        be added here.
        """
        # -- Core state machine --
        self._issue_current_state.pop(issue_id, None)
        self._issue_state_runs.pop(issue_id, None)
        self._pending_gates.pop(issue_id, None)
        self._issue_workflow.pop(issue_id, None)
        self._issue_repo.pop(issue_id, None)
        self._issue_project.pop(issue_id, None)
        self._rejected_issues.discard(issue_id)
        self._migrated_issues.discard(issue_id)
        self._config_blocked.discard(issue_id)
        self._rejection_fetch_pending.discard(issue_id)
        self._prev_issue_labels.pop(issue_id, None)
        # -- Session/timing --
        self._last_session_ids.pop(issue_id, None)
        self._last_completed_at.pop(issue_id, None)
        # -- Issue cache --
        self._last_issues.pop(issue_id, None)
        # -- Retry --
        timer = self._retry_timers.pop(issue_id, None)
        if timer is not None:
            timer.cancel()
        self.retry_attempts.pop(issue_id, None)
        # -- Dispatch --
        self.running.pop(issue_id, None)
        self._tasks.pop(issue_id, None)
        self.claimed.discard(issue_id)
        # -- Scheduled-jobs child reverse-index --
        # If this issue is a scheduled-job child, remove it from its template's
        # index. Idempotent: pop returns None if not a child.
        template_id = self._child_to_template.pop(issue_id, None)
        if template_id:
            children = self._template_children.get(template_id)
            if children is not None:
                children.discard(issue_id)

    async def _cleanup_template_state(self, template_id: str) -> None:
        """Idempotent cleanup of all per-template tracking state.

        Called on template hard-delete detection (Unit 8 completes the
        cascade-cancel of in-flight children). Any new per-template dict
        added to ``__init__`` MUST be mirrored here.
        """
        # Tear down any persistent workspace associated with this template
        # BEFORE dropping the snapshot — the helper needs the snapshot to
        # compute the workspace key.
        try:
            await self._remove_workspace_for_template(template_id)
        except Exception as e:
            logger.warning(
                f"Failed to remove persistent workspace for template {template_id}: {e}"
            )
        self._templates.discard(template_id)
        self._template_snapshots.pop(template_id, None)
        self._template_children.pop(template_id, None)
        self._template_last_fired.pop(template_id, None)
        self._template_last_seen.pop(template_id, None)
        self._template_error_since.pop(template_id, None)
        self._template_watermark_seq.pop(template_id, None)
        self._template_next_fire_at.pop(template_id, None)
        self._template_seq_seeded.discard(template_id)
        # Per-slot fire-attempt entries are keyed by (template_id, slot).
        for key in list(self._template_fire_attempts):
            if key[0] == template_id:
                self._template_fire_attempts.pop(key, None)
        # Note: _child_to_template entries for this template's children are
        # removed when those children terminal-transition via
        # _cleanup_issue_state. Unit 8 extends this to cascade-cancel
        # in-flight children.

    async def _cascade_template_delete(self, template_id: str) -> None:
        """Template hard-delete detected after N-tick threshold (R21).

        Cancels in-flight children via the R8 cancel-previous protocol
        (graceful state transition + audit-trail comments on child) when
        a Canceled state is configured, falling back to the minimal
        kill-only path otherwise. Destroys persistent workspace (if any)
        and fully cleans up per-template tracking state.

        Idempotent: subsequent calls with the same ``template_id`` are
        no-ops because ``_cleanup_template_state`` clears the tracking
        sets the caller iterates.
        """
        logger.warning(
            f"Template hard-delete detected template={template_id} — "
            f"cascading cleanup after "
            f"{TEMPLATE_HARD_DELETE_THRESHOLD_TICKS} consecutive absences"
        )
        # Snapshot template identifier + child ids before mutating
        # tracking — the cancel path will mutate _template_children /
        # _child_to_template / _template_snapshots.
        template_snapshot = self._template_snapshots.get(template_id)
        template_identifier = (
            template_snapshot.identifier if template_snapshot else template_id
        )
        children = list(self._template_children.get(template_id, set()))
        canceled_state_name = (self.cfg.linear_states.canceled or "").strip()

        for child_id in children:
            child_issue = self._last_issues.get(child_id)
            child_identifier = (
                child_issue.identifier if child_issue else child_id
            )
            cancel_protocol_ran = False
            if canceled_state_name:
                try:
                    await self._cancel_child_for_overlap(
                        child_id=child_id,
                        child_identifier=child_identifier,
                        template_id=template_id,
                        template_identifier=template_identifier,
                        triggering_slot=f"template_deleted:{template_identifier}",
                        canceled_state_name=canceled_state_name,
                    )
                    cancel_protocol_ran = True
                except Exception as e:
                    logger.warning(
                        f"cascade_delete: cancel protocol failed for "
                        f"child={child_identifier} template={template_identifier}: {e}",
                        exc_info=True,
                    )
            if not cancel_protocol_ran:
                # Fallback: minimal kill + cleanup. _cancel_child_for_overlap
                # already does both internally, so only run this branch when
                # we didn't invoke it (or it raised before them).
                try:
                    await self._kill_worker(
                        child_id, reason="template_deleted"
                    )
                except Exception as e:
                    logger.warning(
                        f"kill_worker failed for child={child_identifier} "
                        f"during cascade delete of template={template_identifier}: {e}"
                    )
                self._cleanup_issue_state(child_id)
        # _cleanup_template_state also handles _remove_workspace_for_template
        # internally and wipes all per-template tracking.
        await self._cleanup_template_state(template_id)

    async def _rehydrate_template_indexes(self) -> None:
        """After ``_fetch_templates`` populates ``_template_snapshots``,
        rebuild the child reverse index and seed seq counters from Linear.

        Called once on startup. No-op when ``_template_snapshots`` is
        empty. Per-template failures are logged and skipped — one
        transient fetch error must not block the rest of startup.
        """
        if not self._template_snapshots:
            return
        client = self._ensure_linear_client()
        for template_id, template in list(self._template_snapshots.items()):
            try:
                children = await client.fetch_template_children(
                    template_id, include_archived=False
                )
            except Exception as e:
                logger.warning(
                    f"Failed to fetch children for template={template_id} "
                    f"during rehydration: {e}"
                )
                continue
            child_ids = {c.id for c in children}
            self._template_children[template_id] = child_ids
            for cid in child_ids:
                self._child_to_template[cid] = template_id
            # Seed seq counter from existing fire watermarks so a restart
            # doesn't reset to 0 (which would briefly tie new watermarks
            # with pre-restart ones during supersession).
            try:
                await self._seed_seq_from_linear(template)
            except Exception as e:
                logger.debug(
                    f"seq seed failed during rehydration for "
                    f"{template.identifier}: {e}"
                )

    def _resolve_schedule_config_for_template(
        self, template_id: str
    ) -> "ScheduleConfig | None":  # noqa: F821 — forward ref
        """Resolve the ScheduleConfig for a template by snapshot lookup.

        Returns None if the template snapshot is missing or the template
        carries no ``schedule:<name>`` label mapping to a configured
        schedule. Safe to call when ``_template_snapshots`` is empty
        (e.g. during startup before ``_fetch_templates`` has run).
        """
        template = self._template_snapshots.get(template_id)
        if not template:
            return None
        return self._resolve_schedule_config(template)

    async def _remove_workspace_for_child(
        self, issue_id: str, issue_identifier: str
    ) -> None:
        """Route child-terminal workspace cleanup through workspace_mode awareness.

        For children of persistent-mode templates, runs ``before_remove``
        hook but skips directory/volume deletion — the workspace is keyed
        by the template identifier and survives individual child terminals.

        For ephemeral (default) or non-scheduled children, performs
        standard removal keyed by the child's own identifier.

        Safe to call during startup before ``_template_snapshots`` has
        been populated: the template lookup falls through and the call
        behaves like the historical ephemeral path.
        """
        issue_cfg = self._cfg_for_issue_or_primary(issue_id)
        ws_root = issue_cfg.workspace.resolved_root()
        docker_cfg = issue_cfg.docker if issue_cfg.docker.enabled else None
        repo = self._get_issue_repo_config(issue_id)
        rendered_hooks = _render_hooks_best_effort(
            issue_cfg.hooks, repo, issue_cfg.repos_synthesized
        )

        template_id = self._child_to_template.get(issue_id)
        if template_id:
            schedule_cfg = self._resolve_schedule_config_for_template(template_id)
            template = self._template_snapshots.get(template_id)
            if schedule_cfg and template and schedule_cfg.workspace_mode == "persistent":
                # Persistent: preserve the template-keyed workspace across
                # this child's terminal. before_remove still runs.
                await remove_workspace(
                    ws_root,
                    template.identifier,
                    repo.name,
                    rendered_hooks,
                    docker_cfg=docker_cfg,
                    workspace_key=sanitize_key(template.identifier),
                    skip_removal=True,
                )
                return

        # Fallback: ephemeral / non-scheduled child — destroy per-child workspace.
        await remove_workspace(
            ws_root,
            issue_identifier,
            repo.name,
            rendered_hooks,
            docker_cfg=docker_cfg,
        )

    async def _remove_workspace_for_template(self, template_id: str) -> None:
        """Tear down a template's persistent workspace.

        Called from ``_cleanup_template_state`` on template exit. Runs
        the ``before_remove`` hook AND deletes the directory / Docker
        volume. For templates whose schedule is not persistent-mode or
        whose snapshot is missing, this is a no-op.
        """
        template = self._template_snapshots.get(template_id)
        if not template:
            return
        schedule_cfg = self._resolve_schedule_config_for_template(template_id)
        if not schedule_cfg or schedule_cfg.workspace_mode != "persistent":
            return
        ws_root = self.cfg.workspace.resolved_root()
        docker_cfg = self.cfg.docker if self.cfg.docker.enabled else None
        await remove_workspace(
            ws_root,
            template.identifier,
            self.cfg.hooks,
            docker_cfg=docker_cfg,
            workspace_key=sanitize_key(template.identifier),
            skip_removal=False,
        )

    def _fire_and_forget(self, coro) -> None:
        """Schedule a coroutine without awaiting it. Prevents GC of the task."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _client_for_issue(self, issue_id: str) -> LinearClient:
        """Return the LinearClient for the project owning ``issue_id``.

        Falls back to the legacy _ensure_linear_client() when the issue has
        no project binding (pre-bind paths like _post_*_comment called from
        error-handling blocks).
        """
        slug = self._issue_project.get(issue_id)
        if slug and slug in self.configs:
            return self._linear_client_for(slug)
        return self._ensure_linear_client()

    async def _post_cancellation_comment(
        self, issue_id: str, state_name: str
    ) -> None:
        """Post a best-effort tracking comment when an issue is cancelled."""
        try:
            client = self._client_for_issue(issue_id)
            await client.post_comment(
                issue_id,
                f"[Stokowski] Agent terminated — issue moved to {state_name}.",
            )
        except Exception as e:
            logger.debug(f"Failed to post cancellation comment for {issue_id}: {e}")

    async def _post_hook_error_comment(self, issue_id: str, detail: str) -> None:
        """Post a Linear comment when a hook template or hook execution fails."""
        try:
            client = self._client_for_issue(issue_id)
            await client.post_comment(
                issue_id, f"[Stokowski] {detail}"
            )
        except Exception as e:
            logger.debug(f"Failed to post hook-error comment for {issue_id}: {e}")

    async def _cleanup_logs(self) -> None:
        """Run log retention cleanup. Best-effort — errors logged and swallowed.

        Multi-project: iterate each project's logging config. Projects may
        share a log_dir (union the exempt set) or use different dirs.
        """
        exempt = {
            self.running[iid].issue_identifier
            for iid in self.running
            if self.running[iid].issue_identifier
        }
        for slug, parsed in self.configs.items():
            pcfg = parsed.config
            if not pcfg.logging.enabled or not pcfg.logging.log_dir:
                continue
            try:
                workflow_dir = self._config_paths.get(slug, self.workflow_path).parent
                log_dir = pcfg.logging.resolved_log_dir(workflow_dir)
                if not log_dir.exists():
                    continue
                if pcfg.logging.max_age_days > 0:
                    cleanup_old_logs(log_dir, pcfg.logging.max_age_days)
                if pcfg.logging.max_total_size_mb > 0:
                    enforce_size_limit(
                        log_dir, pcfg.logging.max_total_size_mb, exempt
                    )
            except Exception as e:
                logger.warning(f"Log retention cleanup failed for {slug}: {e}")

    async def start(self):
        """Start the orchestration loop."""
        errors_by_slug = self._load_all_workflows()
        if errors_by_slug:
            for slug, errs in errors_by_slug.items():
                for e in errs:
                    logger.error(f"Config error ({slug}): {e}")
            raise RuntimeError(f"Startup validation failed: {errors_by_slug}")

        # Startup filename → project_slug log (operators can see which file
        # supplies shared globals).
        primary_slug = next(iter(self.configs.keys()))
        for slug, path in self._config_paths.items():
            logger.info(
                f"Loaded project file={path} slug={slug} primary={slug == primary_slug}"
            )

        # Docker startup checks — union of all docker-enabled projects.
        docker_enabled_cfgs = [
            p.config for p in self.configs.values() if p.config.docker.enabled
        ]
        if docker_enabled_cfgs:
            ok, msg = await check_docker_available()
            if not ok:
                raise RuntimeError(f"Docker mode enabled but: {msg}")
            # Pre-pull every image across the 3-level hybrid for every
            # docker-enabled project.
            images: set[str] = set()
            for cfg in docker_enabled_cfgs:
                if cfg.docker.default_image:
                    images.add(cfg.docker.default_image)
                for sc in cfg.states.values():
                    if sc.docker_image:
                        images.add(sc.docker_image)
                for repo in cfg.repos.values():
                    if repo.docker_image:
                        images.add(repo.docker_image)
            for img in images:
                logger.info(f"Pulling Docker image: {img}")
                if not await pull_image(img):
                    logger.warning(f"Failed to pull image: {img} (may already be cached)")

        primary = self._primary_cfg()
        logger.info(
            f"Starting Stokowski "
            f"projects={list(self.configs.keys())} "
            f"primary={primary.tracker.project_slug} "
            f"max_agents={primary.agent.max_concurrent_agents} "
            f"poll_ms={self._polling_interval_ms}"
        )

        self._running = True
        self._stop_event = asyncio.Event()

        # Startup terminal cleanup
        await self._startup_cleanup()

        # Main poll loop
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Tick error: {e}")

            # Interruptible sleep — read the cached min polling interval so
            # a hot-reload that changes the min is picked up next tick.
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._polling_interval_ms / 1000,
                )
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # Normal poll interval elapsed

    async def stop(self):
        """Stop the orchestration loop and kill all running agents."""
        self._running = False
        if hasattr(self, '_stop_event'):
            self._stop_event.set()

        # Kill all child claude processes first
        for pid in list(self._child_pids):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        self._child_pids.clear()

        # Kill Docker agent containers by label — if any project has docker
        # enabled, run one cleanup pass (containers are labeled globally).
        any_docker = any(
            p.config.docker.enabled for p in self.configs.values()
        )
        if any_docker or (self.workflow and self.cfg.docker.enabled):
            try:
                await cleanup_orphaned_containers()
            except Exception:
                pass

        # Cancel async tasks
        for issue_id, task in list(self._tasks.items()):
            task.cancel()
        # Give them a moment to finish
        if self._tasks:
            await asyncio.sleep(0.5)
        self._tasks.clear()

        # Close the legacy single-client slot AND every per-project client.
        if self._linear:
            try:
                await self._linear.close()
            except Exception:
                pass
        for slug, client in list(self._linear_clients.items()):
            try:
                await client.close()
            except Exception:
                pass
        self._linear_clients.clear()

    async def _startup_cleanup(self):
        """Remove workspaces for issues already in terminal states.

        Multi-project: iterate each project, fetch its terminal issues via
        its own client, remove workspaces using its repos/hooks. Docker
        container cleanup runs once (containers are labeled globally).
        Volume pruning unions active keys across every docker-enabled project
        and prunes in a single pass.

        Unit 8 extends this to:
          1. Fetch templates + rehydrate reverse index so
             ``_remove_workspace_for_child`` can route persistent-mode
             children correctly (skip removal).
          2. Sweep orphaned Docker volumes via ``cleanup_orphaned_volumes``,
             passing the union of persistent-template keys and in-flight
             active-state child keys so we don't destroy live volumes.
        """
        any_docker = any(p.config.docker.enabled for p in self.configs.values())

        if any_docker:
            count = await cleanup_orphaned_containers()
            if count:
                logger.info(f"Killed {count} orphaned agent containers")

        # Fetch templates + rehydrate BEFORE routing terminal cleanup, so
        # persistent-mode children get the skip-removal branch.
        try:
            await self._fetch_templates()
        except Exception as e:
            logger.warning(f"Startup template fetch failed (continuing): {e}")
        try:
            await self._rehydrate_template_indexes()
        except Exception as e:
            logger.warning(
                f"Startup template index rehydration failed (continuing): {e}"
            )

        # Per-project terminal-issue workspace cleanup + active-state snapshot.
        # Terminal removal routes through _remove_workspace_for_child so
        # persistent-mode children get the skip-removal branch.
        active_issue_identifiers: set[str] = set()
        for slug, parsed in self.configs.items():
            pcfg = parsed.config
            try:
                client = self._linear_client_for(slug)
                terminal = await client.fetch_issues_by_states(
                    pcfg.tracker.project_slug,
                    pcfg.terminal_linear_states(),
                )
                for issue in terminal:
                    await self._remove_workspace_for_child(issue.id, issue.identifier)
                active = await client.fetch_issues_by_states(
                    pcfg.tracker.project_slug,
                    pcfg.active_linear_states(),
                )
                for i in active:
                    if i.identifier:
                        active_issue_identifiers.add(i.identifier)
            except Exception as e:
                logger.warning(f"Startup cleanup failed for {slug} (continuing): {e}")

        # Union Docker volume pruning across docker-enabled projects.
        # Preserves in-flight active-state children (composite keys) and every
        # known template identifier (persistent-mode workspace keys).
        if any_docker:
            try:
                from .workspace import compose_workspace_key

                active_keys: set[str] = set()
                # Active-state composite keys (identifier-repo) across projects.
                for issue_id, issue_obj in self._last_issues.items():
                    if issue_obj.identifier not in active_issue_identifiers:
                        continue
                    slug = self._issue_project.get(issue_id)
                    if slug and slug in self.configs:
                        repos = self.configs[slug].config.repos.values()
                    else:
                        repos = (
                            r
                            for parsed in self.configs.values()
                            for r in parsed.config.repos.values()
                        )
                    for repo in repos:
                        active_keys.add(
                            compose_workspace_key(issue_obj.identifier, repo.name).lower()
                        )
                # Template identifiers — persistent-mode workspace keys.
                # Preserve every known template regardless of mode; ephemeral
                # templates typically have no volume so this is cheap and
                # avoids accidents if an operator flips modes across a restart.
                for template in self._template_snapshots.values():
                    if template.identifier:
                        active_keys.add(sanitize_key(template.identifier))

                # Use the first docker-enabled project's DockerConfig for the
                # volume-cleanup call (volume labels are global).
                docker_cfg_for_cleanup = next(
                    p.config.docker
                    for p in self.configs.values()
                    if p.config.docker.enabled
                )
                removed = await cleanup_orphaned_volumes(docker_cfg_for_cleanup, active_keys)
                if removed:
                    logger.info(
                        f"Startup: removed {removed} orphaned workspace volumes"
                    )
            except Exception as e:
                logger.warning(f"Orphan volume sweep failed (continuing): {e}")

        await self._cleanup_logs()

    def _cfg_for_issue_or_primary(self, issue_id: str) -> ServiceConfig:
        """Variant of _cfg_for_issue that falls back to the primary cfg when
        the issue has no project binding yet (cold-start / pre-bind paths)."""
        slug = self._issue_project.get(issue_id)
        if slug and slug in self.configs:
            return self.configs[slug].config
        return self._primary_cfg()

    def _resolve_workflow(self, issue: Issue) -> WorkflowConfig:
        """Resolve which workflow applies to an issue and cache the result.

        Multi-project: uses the issue's OWN project config (workflows are
        declared per-project).
        """
        cfg = self._cfg_for_issue_or_primary(issue.id)
        workflow = cfg.resolve_workflow(issue)
        self._issue_workflow[issue.id] = workflow.name
        return workflow

    def _get_issue_workflow_config(self, issue_id: str) -> WorkflowConfig:
        """Look up the cached workflow for an issue, with fallbacks."""
        cfg = self._cfg_for_issue_or_primary(issue_id)
        cached_name = self._issue_workflow.get(issue_id)
        if cached_name is not None:
            wf = cfg.get_workflow(cached_name)
            if wf is not None:
                return wf
            cached_issue = self._last_issues.get(issue_id)
            if cached_issue is not None:
                try:
                    return self._resolve_workflow(cached_issue)
                except ValueError:
                    pass
        for wf in cfg.workflows.values():
            if wf.default:
                return wf
        if cfg.workflows:
            return next(iter(cfg.workflows.values()))
        raise RuntimeError("No workflows defined in config")

    def _resolve_repo(self, issue: Issue) -> RepoConfig:
        """Resolve which repo applies to an issue and cache the result."""
        cfg = self._cfg_for_issue_or_primary(issue.id)
        repo = cfg.resolve_repo(issue)
        self._issue_repo[issue.id] = repo.name
        return repo

    def _repo_name_for_tracking(self, issue_id: str) -> str:
        """Return the repo name to carry in a tracking comment payload."""
        return self._get_issue_repo_config(issue_id).name

    def _get_issue_repo_config(self, issue_id: str) -> RepoConfig:
        """Look up the cached repo for an issue, with fallbacks."""
        cfg = self._cfg_for_issue_or_primary(issue_id)
        cached_name = self._issue_repo.get(issue_id)
        if cached_name is not None:
            repo = cfg.repos.get(cached_name)
            if repo is not None:
                return repo
            cached_issue = self._last_issues.get(issue_id)
            if cached_issue is not None:
                try:
                    return self._resolve_repo(cached_issue)
                except ValueError:
                    pass
        for repo in cfg.repos.values():
            if repo.default:
                return repo
        if cfg.repos:
            return next(iter(cfg.repos.values()))
        raise RuntimeError("No repos defined in config")

    def _resolve_gate_workflow(
        self, issue: Issue, tracking: dict | None
    ) -> WorkflowConfig:
        """Resolve workflow for a gated issue, handling cold-start recovery.

        Resolution order:
        1. Cached in ``_issue_workflow`` → use cached workflow
        2. Not cached (cold start after restart):
           a. If ``tracking`` has a ``workflow`` field and it exists in config
              → cache it and use it
           b. Otherwise → resolve from issue labels via ``_resolve_workflow()``

        This must be called before looking up transitions in gate handling,
        so that ``_issue_workflow`` is populated for downstream use.
        """
        # 1. Already cached — use it
        if issue.id in self._issue_workflow:
            return self._get_issue_workflow_config(issue.id)

        cfg = self._cfg_for_issue_or_primary(issue.id)
        # 2. Cold start — try tracking comment's workflow field
        if tracking is not None:
            tracked_wf_name = tracking.get("workflow")
            if tracked_wf_name is not None:
                wf = cfg.get_workflow(tracked_wf_name)
                if wf is not None:
                    self._issue_workflow[issue.id] = wf.name
                    return wf

        # 3. Resolve from issue labels (default fallback)
        return self._resolve_workflow(issue)

    async def _resolve_current_state(self, issue: Issue) -> tuple[str, int]:
        """Resolve current state machine state for an issue.
        Returns (state_name, run).

        Multi-project: reads the issue's own project cfg. ``_issue_project``
        must be stamped before this is called (enforced by ``_tick``/
        ``_handle_gate_responses`` binding ordering).
        """
        cfg = self._cfg_for_issue_or_primary(issue.id)

        # Check state cache first
        if issue.id in self._issue_current_state:
            state_name = self._issue_current_state[issue.id]
            run = self._issue_state_runs.get(issue.id, 1)
            if issue.id not in self._issue_workflow:
                self._resolve_workflow(issue)
            return state_name, run

        is_todo = (
            issue.state
            and issue.state.strip().lower() == cfg.linear_states.todo.strip().lower()
        )
        if is_todo:
            workflow = self._resolve_workflow(issue)
            entry = workflow.entry_state
            if not entry:
                raise RuntimeError(f"No entry state for workflow '{workflow.name}'")
            self._issue_current_state[issue.id] = entry
            self._issue_state_runs[issue.id] = 1
            return entry, 1

        # Fetch comments via the issue's own project client.
        client = self._client_for_issue(issue.id)
        comments = await client.fetch_comments(issue.id)
        tracking = parse_latest_tracking(comments)

        workflow: WorkflowConfig | None = None
        if issue.id in self._issue_workflow:
            workflow = self._get_issue_workflow_config(issue.id)
        elif tracking is not None:
            tracked_wf_name = tracking.get("workflow")
            if tracked_wf_name is not None:
                wf_from_tracking = cfg.get_workflow(tracked_wf_name)
                if wf_from_tracking is not None:
                    workflow = wf_from_tracking
                    self._issue_workflow[issue.id] = workflow.name
            if workflow is None:
                workflow = self._resolve_workflow(issue)
        else:
            workflow = self._resolve_workflow(issue)

        entry = workflow.entry_state
        if not entry:
            raise RuntimeError(
                f"No entry state in workflow '{workflow.name}'"
            )

        await self._resolve_repo_for_coldstart(issue, tracking, comments)

        if tracking is None:
            self._issue_current_state[issue.id] = entry
            self._issue_state_runs[issue.id] = 1
            return entry, 1

        workflow_path_set = set(workflow.path)

        if tracking["type"] == "state":
            state_name = tracking.get("state", entry)
            run = tracking.get("run", 1)
            if state_name in cfg.states and state_name in workflow_path_set:
                self._issue_current_state[issue.id] = state_name
                self._issue_state_runs[issue.id] = run
                return state_name, run
            if state_name in cfg.states:
                logger.info(
                    f"State '{state_name}' not in workflow '{workflow.name}' path, "
                    f"resetting to entry '{entry}' for {issue.identifier}"
                )
            self._issue_current_state[issue.id] = entry
            self._issue_state_runs[issue.id] = 1
            return entry, 1

        if tracking["type"] == "gate":
            gate_state = tracking.get("state", "")
            status = tracking.get("status", "")
            run = tracking.get("run", 1)

            if status == "waiting":
                if gate_state in cfg.states and gate_state in workflow_path_set:
                    self._issue_current_state[issue.id] = gate_state
                    self._issue_state_runs[issue.id] = run
                    self._pending_gates[issue.id] = gate_state
                    return gate_state, run

            elif status == "approved":
                wf_transitions = workflow.transitions.get(gate_state, {})
                target = wf_transitions.get("approve")
                if not target:
                    gate_cfg = cfg.states.get(gate_state)
                    if gate_cfg:
                        target = gate_cfg.transitions.get("approve")
                if target and target in cfg.states:
                    self._issue_current_state[issue.id] = target
                    self._issue_state_runs[issue.id] = run
                    return target, run

            elif status == "rework":
                wf_transitions = workflow.transitions.get(gate_state, {})
                rework_to = tracking.get("rework_to", "")
                if not rework_to:
                    rework_to = wf_transitions.get("rework_to", "")
                if not rework_to:
                    gate_cfg = cfg.states.get(gate_state)
                    if gate_cfg:
                        rework_to = gate_cfg.rework_to or ""
                if rework_to and rework_to in cfg.states:
                    self._issue_current_state[issue.id] = rework_to
                    self._issue_state_runs[issue.id] = run
                    return rework_to, run

        # Fallback to workflow entry state
        self._issue_current_state[issue.id] = entry
        self._issue_state_runs[issue.id] = 1
        return entry, 1

    async def _safe_enter_gate(self, issue: Issue, state_name: str):
        """Wrapper around _enter_gate that logs errors."""
        try:
            await self._enter_gate(issue, state_name)
        except Exception as e:
            logger.error(
                f"Enter gate failed issue={issue.identifier} "
                f"gate={state_name}: {e}",
                exc_info=True,
            )

    async def _enter_gate(self, issue: Issue, state_name: str):
        """Move issue to gate state and post tracking comment."""
        cfg = self._cfg_for_issue_or_primary(issue.id)
        state_cfg = cfg.states.get(state_name)
        workflow = self._get_issue_workflow_config(issue.id)

        # Check for skip labels — auto-approve if any match
        if state_cfg and state_cfg.skip_labels:
            issue_labels_lower = [l.lower() for l in (issue.labels or [])]
            skip_labels_lower = [s.lower() for s in state_cfg.skip_labels]
            should_skip = any(sl in issue_labels_lower for sl in skip_labels_lower)

            # Resolve approve target from workflow transitions
            state_transitions = workflow.transitions.get(state_name, {})
            approve_target = state_transitions.get("approve")

            if should_skip and approve_target:
                target = approve_target
                run = self._issue_state_runs.get(issue.id, 1)

                client = self._client_for_issue(issue.id)
                repo_name = self._repo_name_for_tracking(issue.id)
                comment = make_gate_comment(
                    state=state_name, status="approved", run=run,
                    workflow=workflow.name, repo=repo_name,
                )
                await client.post_comment(issue.id, comment)

                self._issue_current_state[issue.id] = target
                self._issue_state_runs[issue.id] = 1  # Reset for new state
                self.running.pop(issue.id, None)
                self._tasks.pop(issue.id, None)
                # Keep claimed — prevents double-dispatch race with concurrent tick
                self._pending_gates.pop(issue.id, None)

                # Post state-entry comment for audit trail
                state_comment = make_state_comment(
                    state=target, run=1, workflow=workflow.name,
                    repo=repo_name,
                )
                await client.post_comment(issue.id, state_comment)

                logger.info(
                    f"Gate auto-skipped issue={issue.identifier} "
                    f"gate={state_name} (label match) -> {target}"
                )
                self._schedule_retry(issue, attempt_num=0, delay_ms=1000)
                return

        prompt = state_cfg.prompt if state_cfg else ""
        run = self._issue_state_runs.get(issue.id, 1)

        client = self._client_for_issue(issue.id)
        issue_cfg = self._cfg_for_issue_or_primary(issue.id)

        comment = make_gate_comment(
            state=state_name,
            status="waiting",
            prompt=prompt or "",
            run=run,
            workflow=workflow.name,
            repo=self._repo_name_for_tracking(issue.id),
        )
        await client.post_comment(issue.id, comment)

        review_state = issue_cfg.linear_states.review
        moved = await client.update_issue_state(issue.id, review_state)
        if not moved:
            logger.error(
                f"Failed to move {issue.identifier} to review state '{review_state}' "
                f"— issue will remain claimed to prevent re-dispatch loop"
            )
            # Keep claimed so the issue doesn't get re-dispatched while
            # still in the active Linear state. Track the gate so
            # _handle_gate_responses can pick it up if the state is
            # changed manually.
            self._pending_gates[issue.id] = state_name
            self._issue_current_state[issue.id] = state_name
            self.running.pop(issue.id, None)
            self._tasks.pop(issue.id, None)
            # Schedule a retry to attempt the state move again
            self._schedule_retry(issue, attempt_num=0, delay_ms=10_000)
            return

        self._pending_gates[issue.id] = state_name
        self._issue_current_state[issue.id] = state_name
        # Release from running/claimed so it doesn't block slots
        self.running.pop(issue.id, None)
        self._tasks.pop(issue.id, None)
        self.claimed.discard(issue.id)

        logger.info(
            f"Gate entered issue={issue.identifier} gate={state_name} "
            f"run={run}"
        )

    async def _safe_transition(self, issue: Issue, transition_name: str):
        """Wrapper around _transition that logs errors instead of silently swallowing them."""
        try:
            await self._transition(issue, transition_name)
        except Exception as e:
            logger.error(
                f"Transition failed issue={issue.identifier} "
                f"transition={transition_name}: {e}",
                exc_info=True,
            )
            # Release claimed so the issue can be retried on next tick
            self.claimed.discard(issue.id)

    async def _transition(self, issue: Issue, transition_name: str):
        """Follow a transition from the current state.

        Uses workflow-specific transitions instead of StateConfig.transitions.

        Handles target types:
        - terminal → move to workflow-configured terminal state, clean workspace, release tracking
        - gate → enter gate
        - agent → post state comment, ensure active Linear state, schedule retry
        """
        issue_cfg = self._cfg_for_issue_or_primary(issue.id)
        current_state_name = self._issue_current_state.get(issue.id)
        if not current_state_name:
            logger.warning(f"No current state for {issue.identifier}, cannot transition")
            self.claimed.discard(issue.id)
            return

        current_cfg = issue_cfg.states.get(current_state_name)
        if not current_cfg:
            logger.warning(f"Unknown state '{current_state_name}' for {issue.identifier}")
            self.claimed.discard(issue.id)
            return

        workflow = self._get_issue_workflow_config(issue.id)
        state_transitions = workflow.transitions.get(current_state_name, {})

        target_name = state_transitions.get(transition_name)
        if not target_name:
            logger.warning(
                f"No '{transition_name}' transition from state '{current_state_name}' "
                f"for {issue.identifier}, falling back to 'complete'"
            )
            target_name = state_transitions.get("complete")
            if not target_name:
                self.claimed.discard(issue.id)
                return
            transition_name = "complete"

        target_cfg = issue_cfg.states.get(target_name)
        if not target_cfg:
            logger.warning(f"Transition target '{target_name}' not found in config")
            self.claimed.discard(issue.id)
            return

        run = self._issue_state_runs.get(issue.id, 1)

        if target_cfg.type == "terminal":
            terminal_key = workflow.terminal_state
            terminal_state = _resolve_linear_state_name(terminal_key, issue_cfg.linear_states)
            try:
                client = self._client_for_issue(issue.id)
                moved = await client.update_issue_state(issue.id, terminal_state)
                if moved:
                    logger.info(f"Moved {issue.identifier} to terminal state '{terminal_state}'")
                else:
                    logger.warning(f"Failed to move {issue.identifier} to terminal state '{terminal_state}'")
            except Exception as e:
                logger.warning(f"Failed to move {issue.identifier} to terminal: {e}")
            try:
                await self._remove_workspace_for_child(issue.id, issue.identifier)
            except Exception as e:
                logger.warning(f"Failed to remove workspace for {issue.identifier}: {e}")
            self._cleanup_issue_state(issue.id)
            self.completed.add(issue.id)

        elif target_cfg.type == "gate":
            self._issue_current_state[issue.id] = target_name
            await self._enter_gate(issue, target_name)

        else:
            self._issue_current_state[issue.id] = target_name

            if transition_name != "complete":
                run = run + 1
                self._issue_state_runs[issue.id] = run
            else:
                run = 1
                self._issue_state_runs[issue.id] = run

            client = self._client_for_issue(issue.id)
            comment = make_state_comment(
                state=target_name,
                run=run,
                workflow=workflow.name,
                repo=self._repo_name_for_tracking(issue.id),
            )
            await client.post_comment(issue.id, comment)

            active_state = issue_cfg.linear_states.active
            moved = await client.update_issue_state(issue.id, active_state)
            if not moved:
                logger.warning(f"Failed to move {issue.identifier} to active state '{active_state}'")

            self._schedule_retry(issue, attempt_num=0, delay_ms=1000)

    async def _handle_gate_responses(self):
        """Check for gate-approved and rework issues, handle transitions.

        Multi-project cold-start ordering (critical): for each project we
        fetch gate issues with THAT project's client, stamp
        ``_issue_project`` before any downstream ``_cfg_for_issue`` call,
        and then run the existing per-issue logic which reads cfg via
        ``_cfg_for_issue(issue.id)``. Gate states (review / gate_approved /
        rework) are not in ``active_linear_states()`` so ``_tick``'s
        candidate fetch cannot stamp them — this method is the sole
        binding site for gate issues after orchestrator restart.
        """
        # Iterate per project — skip projects without gates.
        approved_pairs: list[tuple[Issue, str]] = []  # (issue, project_slug)
        rework_pairs: list[tuple[Issue, str]] = []

        for slug, parsed in list(self.configs.items()):
            pcfg = parsed.config
            if not any(sc.type == "gate" for sc in pcfg.states.values()):
                continue
            try:
                client = self._linear_client_for(slug)
            except RuntimeError:
                continue

            # Fetch gate-approved issues for this project
            try:
                approved_issues = await client.fetch_issues_by_states(
                    pcfg.tracker.project_slug,
                    [pcfg.linear_states.gate_approved],
                )
            except Exception as e:
                logger.warning(f"Failed to fetch gate-approved issues ({slug}): {e}")
                approved_issues = []
            for issue in approved_issues:
                # Bind BEFORE any caller may invoke _cfg_for_issue.
                self._issue_project[issue.id] = slug
                approved_pairs.append((issue, slug))

            # Fetch rework issues for this project
            try:
                rework_issues = await client.fetch_issues_by_states(
                    pcfg.tracker.project_slug,
                    [pcfg.linear_states.rework],
                )
            except Exception as e:
                logger.warning(f"Failed to fetch rework issues ({slug}): {e}")
                rework_issues = []
            for issue in rework_issues:
                self._issue_project[issue.id] = slug
                rework_pairs.append((issue, slug))

        # Process gate approvals
        for issue, slug in approved_pairs:
            if issue.id in self.running or issue.id in self.claimed:
                continue

            client = self._linear_client_for(slug)
            issue_cfg = self._cfg_for_issue(issue.id)

            gate_state = self._pending_gates.pop(issue.id, None)
            tracking: dict | None = None
            comments: list[dict] | None = None
            if not gate_state:
                comments = await client.fetch_comments(issue.id)
                tracking = parse_latest_tracking(comments)
                if tracking and tracking.get("type") == "gate" and tracking.get("status") == "waiting":
                    gate_state = tracking.get("state", "")

            if gate_state:
                try:
                    workflow = self._resolve_gate_workflow(issue, tracking)

                    if issue.id not in self._issue_repo:
                        if comments is None:
                            comments = await client.fetch_comments(issue.id)
                            tracking = tracking or parse_latest_tracking(comments)
                        await self._resolve_repo_for_coldstart(
                            issue, tracking, comments,
                        )

                    run = self._issue_state_runs.get(issue.id, 1)
                    repo_name = self._repo_name_for_tracking(issue.id)
                    comment = make_gate_comment(
                        state=gate_state, status="approved", run=run,
                        workflow=workflow.name, repo=repo_name,
                    )
                    await client.post_comment(issue.id, comment)

                    self._issue_current_state[issue.id] = gate_state
                    wf_transitions = workflow.transitions.get(gate_state, {})
                    target = wf_transitions.get("approve")
                    if not target:
                        gate_cfg = issue_cfg.states.get(gate_state)
                        if gate_cfg and "approve" in gate_cfg.transitions:
                            target = gate_cfg.transitions["approve"]
                    if not target:
                        logger.warning(
                            f"Gate '{gate_state}' has no approve target for "
                            f"{issue.identifier}, skipping"
                        )
                        continue
                    self._issue_current_state[issue.id] = target
                    self._issue_state_runs[issue.id] = 1

                    state_comment = make_state_comment(
                        state=target, run=1, workflow=workflow.name,
                        repo=repo_name,
                    )
                    await client.post_comment(issue.id, state_comment)

                    active_state = issue_cfg.linear_states.active
                    moved = await client.update_issue_state(issue.id, active_state)
                    if moved:
                        issue.state = active_state
                    else:
                        logger.warning(f"Failed to move {issue.identifier} to active after gate approval")
                    self._last_issues[issue.id] = issue
                    logger.info(f"Gate approved issue={issue.identifier} gate={gate_state}")
                except Exception as e:
                    logger.error(
                        f"Gate approval handling failed for {issue.identifier}: {e}",
                        exc_info=True,
                    )

        # Process rework
        for issue, slug in rework_pairs:
            if issue.id in self.running or issue.id in self.claimed:
                continue

            client = self._linear_client_for(slug)
            issue_cfg = self._cfg_for_issue(issue.id)

            gate_state = self._pending_gates.pop(issue.id, None)
            tracking: dict | None = None
            comments: list[dict] | None = None
            if not gate_state:
                # Cold-start fallback: fetch comments to find gate state
                comments = await client.fetch_comments(issue.id)
                tracking = parse_latest_tracking(comments)
                if tracking and tracking.get("type") == "gate" and tracking.get("status") == "waiting":
                    gate_state = tracking.get("state", "")

            if gate_state:
                try:
                    # Resolve workflow (cold-start recovery if needed)
                    workflow = self._resolve_gate_workflow(issue, tracking)

                    # Resolve repo too — see COR-003 rationale in the
                    # approval path above.
                    if issue.id not in self._issue_repo:
                        if comments is None:
                            comments = await client.fetch_comments(issue.id)
                            tracking = tracking or parse_latest_tracking(comments)
                        await self._resolve_repo_for_coldstart(
                            issue, tracking, comments,
                        )

                    gate_cfg = issue_cfg.states.get(gate_state)

                    # Resolve rework_to from workflow transitions, then StateConfig fallback
                    wf_transitions = workflow.transitions.get(gate_state, {})
                    rework_to = wf_transitions.get("rework_to", "")
                    if not rework_to:
                        rework_to = gate_cfg.rework_to if gate_cfg else ""
                    if not rework_to:
                        logger.warning(f"Gate {gate_state} has no rework_to target, skipping")
                        continue

                    # Check max_rework
                    run = self._issue_state_runs.get(issue.id, 1)
                    max_rework = gate_cfg.max_rework if gate_cfg else None
                    repo_name = self._repo_name_for_tracking(issue.id)
                    if max_rework is not None and run >= max_rework:
                        # Exceeded max rework — post escalated comment, don't transition
                        comment = make_gate_comment(
                            state=gate_state, status="escalated", run=run,
                            workflow=workflow.name, repo=repo_name,
                        )
                        await client.post_comment(issue.id, comment)
                        logger.warning(
                            f"Max rework exceeded issue={issue.identifier} "
                            f"gate={gate_state} run={run} max={max_rework}"
                        )
                        continue

                    new_run = run + 1
                    self._issue_state_runs[issue.id] = new_run

                    comment = make_gate_comment(
                        state=gate_state, status="rework",
                        rework_to=rework_to, run=new_run,
                        workflow=workflow.name, repo=repo_name,
                    )
                    await client.post_comment(issue.id, comment)

                    # Post state-entry comment for rework target
                    state_comment = make_state_comment(
                        state=rework_to, run=new_run,
                        workflow=workflow.name, repo=repo_name,
                    )
                    await client.post_comment(issue.id, state_comment)

                    self._issue_current_state[issue.id] = rework_to

                    active_state = issue_cfg.linear_states.active
                    moved = await client.update_issue_state(issue.id, active_state)
                    if moved:
                        issue.state = active_state
                    else:
                        logger.warning(f"Failed to move {issue.identifier} to active after rework")
                    self._last_issues[issue.id] = issue
                    logger.info(
                        f"Rework issue={issue.identifier} gate={gate_state} "
                        f"rework_to={rework_to} run={new_run}"
                    )
                except Exception as e:
                    logger.error(
                        f"Gate rework handling failed for {issue.identifier}: {e}",
                        exc_info=True,
                    )

    async def _fetch_templates(self) -> None:
        """Populate ``self._templates`` and ``self._template_snapshots`` from Linear.

        Runs early in ``_tick`` BEFORE ``_reconcile`` so that reconcile's
        R21 hard-delete detection (Unit 8) operates on a fresh template set.

        On transient fetch failure, the existing snapshots are left untouched
        so reconcile does not misinterpret a single Linear hiccup as a mass
        hard-delete. On clean "no schedules configured" the snapshots are
        cleared so stale entries don't leak after a config edit.
        """
        if not self.cfg.schedules:
            # No schedule types configured — nothing to fetch.
            self._templates.clear()
            self._template_snapshots.clear()
            return

        schedule_state_names = self.cfg.schedule_template_linear_states()
        try:
            client = self._ensure_linear_client()
            templates = await client.fetch_template_issues(
                project_slug=self.cfg.tracker.project_slug,
                schedule_state_names=schedule_state_names,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch templates: {e}", exc_info=True)
            return  # leave existing state untouched on transient failure

        self._templates = {t.id for t in templates}
        self._template_snapshots = {t.id: t for t in templates}

    # ------------------------------------------------------------------
    # Scheduled-jobs evaluator + fire materialization (Unit 6)
    # ------------------------------------------------------------------

    def _next_seq(self, template_id: str) -> int:
        """Return the next watermark ``seq`` for ``template_id`` (1-indexed).

        Monotonic per template. Used as the intra-millisecond tiebreak on
        watermark supersession; the authoritative ordering is still the
        comment timestamp assigned by Linear.
        """
        n = self._template_watermark_seq.get(template_id, 0) + 1
        self._template_watermark_seq[template_id] = n
        return n

    def _resolve_schedule_name(self, template: Issue) -> str | None:
        """Return the schedule-type name encoded in the template's labels.

        A template carries exactly one ``schedule:<name>`` label (Unit 5
        validates this via ``detect_duplicate_label``). Returns the bare
        ``<name>`` portion, lowercased to match how labels are stored.
        Returns None if no schedule label is present.
        """
        for label in (template.labels or []):
            ll = (label or "").lower()
            if ll.startswith("schedule:"):
                name = ll[len("schedule:"):]
                if name:
                    return name
        return None

    def _resolve_schedule_config(
        self, template: Issue
    ) -> "ScheduleConfig | None":  # noqa: F821 — forward ref
        """Look up the ScheduleConfig for a template, with case-insensitive match.

        Config keys in ``self.cfg.schedules`` preserve operator casing;
        labels in Linear are normalized to lowercase on fetch. We retry
        with lowercase keys so a ``schedule:daily_report`` label matches
        a ``daily_report`` config block regardless of casing.
        """
        name = self._resolve_schedule_name(template)
        if not name:
            return None
        schedules = self.cfg.schedules
        if name in schedules:
            return schedules[name]
        # Case-insensitive fallback — Linear lowercases labels.
        for key, sc in schedules.items():
            if key.lower() == name.lower():
                return sc
        return None

    async def _seed_seq_from_linear(
        self, template: Issue, comments: list[dict] | None = None
    ) -> None:
        """One-shot rehydrate ``_template_watermark_seq`` from Linear comments.

        Invoked the first time we see a template in this process lifetime
        so restart doesn't reset seq to 0 (which would briefly tie new
        watermarks with pre-restart ones during supersession). Fetches
        comments only if the caller didn't already.
        """
        if template.id in self._template_seq_seeded:
            return
        try:
            if comments is None:
                client = self._ensure_linear_client()
                comments = await client.fetch_comments(template.id)
            parsed = parse_fired_by_slot(comments or [])
            seeded = max_seq_from_parsed(parsed)
            if seeded > self._template_watermark_seq.get(template.id, 0):
                self._template_watermark_seq[template.id] = seeded
        except Exception as e:
            # Best-effort; a fresh seq counter is safe, just not optimal.
            logger.debug(
                f"seq seeding failed for {template.identifier}: {e}"
            )
        finally:
            self._template_seq_seeded.add(template.id)

    async def _safe_evaluate_schedules(self) -> None:
        """Wrapper matching the ``_safe_transition`` pattern at line ~700.

        Per-template evaluator failures must NOT abort the whole pass —
        those are caught inside ``_evaluate_schedules``. This wrapper
        catches only unexpected exceptions around the loop itself.
        """
        try:
            await self._evaluate_schedules()
        except Exception as e:
            logger.error(f"Schedule evaluator pass failed: {e}", exc_info=True)

    async def _evaluate_schedules(self) -> None:
        """Per-tick schedule evaluation + fire materialization.

        Runs AFTER ``_handle_gate_responses`` and BEFORE
        ``fetch_candidate_issues`` in ``_tick`` so children created here
        are picked up by the same tick's dispatch pass — avoids a full
        poll interval of latency.

        Pipeline per template:
          1. Build a ``TemplateSnapshot`` for the pure evaluator.
          2. Seed seq counter on first sight (rehydrate from Linear).
          3. Fetch fire-watermark comments; parse via ``parse_fired_by_slot``.
          4. Count in-flight children from ``self._template_children``.
          5. Call ``scheduler.evaluate_template(...)`` → list of decisions.
          6. Route each decision to ``_materialize_fire`` or
             ``_post_skip_watermark``.
          7. Apply ``detect_duplicate_label`` across all templates; move
             losers to Error.

        Evaluator or Linear errors for one template are logged and do not
        abort the pass.
        """
        if not self.cfg.schedules:
            return
        if not self._template_snapshots:
            return

        # R18 recovery: clear error-since for templates an operator moved
        # back to Scheduled. Must run BEFORE any ``_move_template_to_error``
        # calls below so a fresh error doesn't get its timestamp reset.
        self._clear_error_state_on_recovery()

        client = self._ensure_linear_client()
        now = datetime.now(timezone.utc)

        # Build TemplateSnapshots for duplicate-label detection. Run this
        # first so losers are moved to Error before we waste API calls
        # fetching their comments / firing them.
        all_snapshots: list[scheduler.TemplateSnapshot] = []
        snapshot_by_id: dict[str, scheduler.TemplateSnapshot] = {}
        for tmpl in self._template_snapshots.values():
            snap = scheduler.TemplateSnapshot(
                id=tmpl.id,
                identifier=tmpl.identifier,
                linear_state=tmpl.state or "",
                cron_expr=tmpl.cron_expr or "",
                timezone=tmpl.timezone or "UTC",
                labels=tuple(tmpl.labels or ()),
                created_at=tmpl.created_at,
            )
            all_snapshots.append(snap)
            snapshot_by_id[tmpl.id] = snap

        try:
            winners, losers = scheduler.detect_duplicate_label(all_snapshots)
        except Exception as e:
            logger.error(f"detect_duplicate_label failed: {e}", exc_info=True)
            winners, losers = all_snapshots, []

        loser_ids = {l.id for l in losers}
        for loser_snap in losers:
            tmpl = self._template_snapshots.get(loser_snap.id)
            if tmpl is None:
                continue
            await self._move_template_to_error(
                tmpl, reason="duplicate_schedule_label",
                details=f"duplicate schedule label on {tmpl.identifier}",
            )

        # Per-template eval loop.
        for template_id, template in list(self._template_snapshots.items()):
            if template_id in loser_ids:
                continue
            snap = snapshot_by_id.get(template_id)
            if snap is None:
                continue

            schedule_cfg = self._resolve_schedule_config(template)
            if schedule_cfg is None:
                # No matching schedule in config — likely a stale label
                # that no longer maps to a config block. Surface once and
                # move the template to Error so the operator notices.
                await self._move_template_to_error(
                    template,
                    reason="schedule_type_removed",
                    details=(
                        f"template label does not match any configured "
                        f"schedule; label is probably stale after a "
                        f"workflow.yaml edit"
                    ),
                )
                continue

            # Cron + timezone must be present. If either is missing the
            # evaluator would raise; surface as a schedule-error instead.
            if not snap.cron_expr or not snap.timezone:
                await self._move_template_to_error(
                    template,
                    reason="missing_cron_or_timezone",
                    details=(
                        f"template {template.identifier} is missing cron "
                        f"expression or timezone custom field"
                    ),
                )
                continue

            # One-shot seq seeding + comment fetch. We fetch comments
            # every tick here — the watermark scan needs them anyway.
            try:
                comments = await client.fetch_comments(template_id)
            except Exception as e:
                logger.warning(
                    f"Failed to fetch comments for template "
                    f"{template.identifier}: {e}"
                )
                continue
            await self._seed_seq_from_linear(template, comments)

            parsed = parse_fired_by_slot(comments or [])
            watermarks = watermarks_from_parsed(parsed, template_id)

            in_flight = len(self._template_children.get(template_id, set()))

            try:
                decisions = scheduler.evaluate_template(
                    snap,
                    watermarks,
                    in_flight,
                    now,
                    schedule_cfg,
                )
            except scheduler.CronParseError as e:
                await self._move_template_to_error(
                    template, reason="cron_parse_error", details=str(e),
                )
                continue
            except scheduler.TimezoneError as e:
                await self._move_template_to_error(
                    template, reason="timezone_error", details=str(e),
                )
                continue
            except Exception as e:
                logger.error(
                    f"Evaluator raised for template "
                    f"{template.identifier}: {e}",
                    exc_info=True,
                )
                continue

            for decision in decisions:
                try:
                    if decision.action == "fire":
                        await self._materialize_fire(template, decision)
                    else:
                        await self._post_skip_watermark(template, decision)
                except Exception as e:
                    logger.error(
                        f"Fire materialization failed for "
                        f"template={template.identifier} slot={decision.slot} "
                        f"action={decision.action}: {e}",
                        exc_info=True,
                    )

    async def _post_skip_watermark(
        self, template: Issue, decision: "scheduler.FireDecision"  # noqa: F821
    ) -> None:
        """Post a terminal skip watermark. No child is created.

        For ``skip_bounded`` with a non-zero drop count we also post the
        aggregate ``bounded_drop`` comment — but only on the first
        dropped slot of the batch, per the evaluator's convention of
        embedding the aggregate on the earliest decision.
        """
        action_to_status = {
            "skip_overlap": "skipped_overlap",
            "skip_bounded": "skipped_bounded",
            "skip_paused": "skipped_paused",
            "skip_error": "skipped_error",
        }
        status = action_to_status.get(decision.action)
        if status is None:
            logger.debug(
                f"Unknown skip action {decision.action} for "
                f"{template.identifier}; ignoring"
            )
            return

        client = self._ensure_linear_client()
        seq = self._next_seq(template.id)
        body = make_fired_comment(
            template_id=template.identifier,
            slot=decision.slot,
            status=status,
            reason=decision.reason,
            seq=seq,
        )
        await client.post_comment(template.id, body)
        logger.info(
            f"Skip watermark template={template.identifier} "
            f"slot={decision.slot} status={status} "
            f"reason={decision.reason}"
        )

        if decision.action == "skip_bounded" and decision.bounded_dropped_count > 0:
            try:
                drop_body = make_bounded_drop_comment(
                    template_id=template.identifier,
                    dropped_count=decision.bounded_dropped_count,
                    earliest_slot=decision.bounded_drop_earliest or decision.slot,
                    latest_slot=decision.bounded_drop_latest or decision.slot,
                )
                await client.post_comment(template.id, drop_body)
            except Exception as e:
                logger.warning(
                    f"Failed to post bounded_drop comment for "
                    f"{template.identifier}: {e}"
                )

    async def _retry_mutation(
        self,
        coro_factory,  # callable returning a fresh awaitable each attempt
        *,
        retries: int = 3,
        backoffs: tuple[float, ...] = (0.1, 0.3, 0.9),
        label: str = "mutation",
    ) -> bool:
        """Call ``coro_factory`` up to ``retries`` times with short backoff.

        The factory must return a *fresh* awaitable on each call — an
        already-awaited coroutine cannot be re-awaited. Returns True on
        the first success; returns False if all retries exhaust. Transient
        exceptions are caught and logged at debug; the method never raises.

        Used by the R8 ``cancel_previous`` three-mutation protocol — each
        of the (state-transition, child-comment, template-reference)
        mutations gets its own retry loop so a partial failure doesn't
        silently skip the remaining steps.
        """
        for i in range(max(1, retries)):
            try:
                result = await coro_factory()
                if result is False:
                    # Best-effort signal from Linear client — don't retry
                    # on a hard-no response (e.g., state-not-found) to
                    # avoid log spam; caller sees False via return.
                    logger.debug(
                        f"_retry_mutation {label!r} attempt {i+1}/{retries}: "
                        f"factory returned False"
                    )
                else:
                    return True
            except Exception as e:
                logger.debug(
                    f"_retry_mutation {label!r} attempt {i+1}/{retries} "
                    f"raised: {e}"
                )
            if i + 1 < retries:
                delay = backoffs[i] if i < len(backoffs) else backoffs[-1]
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
        return False

    async def _cancel_child_for_overlap(
        self,
        child_id: str,
        child_identifier: str,
        template_id: str,
        template_identifier: str,
        triggering_slot: str,
        canceled_state_name: str,
        *,
        replacement_child_id: str | None = None,
    ) -> bool:
        """Three-mutation cancel protocol for R8 ``cancel_previous``.

        Mutations (each independently retried up to 3x):
          1. Move child to Canceled Linear state, unless it is already at
             a terminal state — in that case skip the state change but
             still post the cancel comments with ``already_terminaled`` so
             the audit trail reflects the race.
          2. Post the canonical cancel-tracking comment on the CHILD.
             This is the reviewer's primary notification.
          3. Post a short reference comment on the TEMPLATE.
          4. (Best-effort, future) Post a close comment on any linked PR.
             Deliberately deferred; the plan notes PR-link detection is
             not gating this unit.

        Returns True only if all attempted mutations succeeded. Partial
        failures are logged but do NOT block the caller — the new fire is
        created regardless. The resulting orphan is visible via the
        dashboard.
        """
        if not canceled_state_name:
            logger.error(
                f"_cancel_child_for_overlap: canceled_state_name is empty "
                f"for child={child_identifier} template={template_identifier}; "
                f"refusing to cancel (config invariant violated)"
            )
            return False

        client = self._ensure_linear_client()
        succeeded = True

        # --- Mutation 1: transition child state (unless already terminal) --
        already_terminal = self._is_child_already_terminal(child_id)
        if already_terminal:
            logger.info(
                f"cancel_previous: child={child_identifier} already "
                f"terminal — skipping state transition (preserves audit)"
            )
        else:
            ok = await self._retry_mutation(
                lambda: client.update_issue_state(
                    child_id, canceled_state_name
                ),
                retries=3,
                label=f"cancel_state_transition({child_identifier})",
            )
            if not ok:
                logger.warning(
                    f"cancel_previous: state transition failed for "
                    f"child={child_identifier} after retries; orphan visible "
                    f"on dashboard"
                )
                succeeded = False

        # --- Mutation 2: cancel comment on child ----------------------------
        try:
            child_body = make_cancel_comment(
                child_id=child_identifier,
                reason="overlap",
                triggering_slot=triggering_slot,
                template_id=template_identifier,
                already_terminaled=already_terminal,
                replacement_child_id=replacement_child_id,
            )
            ok = await self._retry_mutation(
                lambda: client.post_comment(child_id, child_body),
                retries=3,
                label=f"cancel_child_comment({child_identifier})",
            )
            if not ok:
                logger.warning(
                    f"cancel_previous: child-comment post failed for "
                    f"{child_identifier} after retries"
                )
                succeeded = False
        except Exception as e:
            logger.warning(
                f"cancel_previous: child-comment build failed "
                f"child={child_identifier}: {e}"
            )
            succeeded = False

        # --- Mutation 3: reference comment on template ----------------------
        try:
            ref_body = make_cancel_reference_comment(
                template_id=template_identifier,
                canceled_child_identifier=child_identifier,
                triggering_slot=triggering_slot,
                already_terminaled=already_terminal,
            )
            ok = await self._retry_mutation(
                lambda: client.post_comment(template_id, ref_body),
                retries=3,
                label=f"cancel_template_ref({template_identifier})",
            )
            if not ok:
                logger.warning(
                    f"cancel_previous: template-reference comment failed for "
                    f"template={template_identifier} after retries"
                )
                succeeded = False
        except Exception as e:
            logger.warning(
                f"cancel_previous: template-ref build failed "
                f"template={template_identifier}: {e}"
            )
            succeeded = False

        # Kill any running worker + clear per-issue tracking so the slot is
        # freed for the new child. _kill_worker + _cleanup_issue_state are
        # both idempotent; safe even when the child was only gated.
        try:
            await self._kill_worker(
                child_id, reason=f"cancel_previous slot={triggering_slot}"
            )
        except Exception as e:
            logger.debug(
                f"cancel_previous: _kill_worker no-op/failed for "
                f"child={child_identifier}: {e}"
            )
        self._cleanup_issue_state(child_id)

        return succeeded

    def _is_child_already_terminal(self, child_id: str) -> bool:
        """Best-effort check: has the child already terminaled naturally?

        Consults cached state (``self._last_issues`` and
        ``self._issue_current_state``). Returns False when uncertain —
        callers treat False as "go ahead and attempt the state
        transition" which is the safer default for the cancel protocol.
        """
        issue = self._last_issues.get(child_id)
        if issue is not None:
            state_type = (issue.state_type or "").strip().lower()
            if state_type in ("completed", "canceled"):
                return True
            state_name = (issue.state or "").strip()
            if state_name and state_name in self.cfg.linear_states.terminal:
                return True
        # Fall back to the internal state machine cache — if we've already
        # cleaned up the issue, odds are it reached a terminal state.
        if child_id not in self._issue_current_state and child_id not in self.running:
            # Not conclusive — could also be a brand-new child not yet
            # tracked. Return False; the worst case is a redundant state
            # transition attempt that Linear will accept.
            return False
        return False

    async def _materialize_fire(
        self, template: Issue, decision: "scheduler.FireDecision"  # noqa: F821
    ) -> None:
        """Materialize a ``fire`` decision into a Linear child issue.

        Implements the R20 idempotency protocol (step order is load-bearing):

            1. Duplicate-sibling check FIRST. If a child already carries
               the ``slot:<canonical>`` label, post
               ``status=child id=<existing>`` and return. This recovers
               from crash-between-create-and-watermark-update without
               posting an orphan pending.
            2. Post ``pending attempt=<n>`` watermark.
            3. Create the child via ``create_child_issue``.
            4. On success: post ``status=child id=<new>`` watermark +
               clear the fire-attempt counter.
            5. On transient failure: post ``status=failed attempt=<n>
               reason=<code>`` + increment the fire-attempt counter.
            6. On permanent failure OR attempts >= MAX: post
               ``status=failed_permanent reason=<code>`` and move the
               template to Error.

        Recovery semantics:

          * Crash between step 1 and step 2 (no watermark, no child):
            next tick evaluator re-decides; idempotent.
          * Crash between step 2 and step 3 (pending watermark, no child):
            next tick's step 1 finds no sibling, step 3 re-runs with
            incremented attempt counter.
          * Crash between step 3 and step 4 (child exists, watermark
            still pending): next tick's step 1 finds the sibling and
            promotes the watermark directly — no duplicate created.
          * Crash between step 5 and next tick: state is as-intended;
            retry proceeds.
        """
        client = self._ensure_linear_client()
        slot = decision.slot
        key = (template.id, slot)
        attempts_before = self._template_fire_attempts.get(key, 0)

        # --- Step 1: duplicate-sibling check ----------------------------
        try:
            siblings = await client.fetch_template_children(
                template.id, include_archived=False
            )
        except Exception as e:
            logger.warning(
                f"fetch_template_children failed for "
                f"{template.identifier}: {e}; skipping this fire"
            )
            return

        active_siblings = [
            c for c in siblings
            if c.archived_at is None
            and (c.state_type or "").lower() not in ("completed", "canceled")
        ]

        existing = find_existing_child_for_slot(active_siblings, slot)
        if existing is not None:
            seq = self._next_seq(template.id)
            body = make_fired_comment(
                template_id=template.identifier,
                slot=slot,
                status="child",
                child_id=existing.identifier or existing.id,
                seq=seq,
            )
            await client.post_comment(template.id, body)
            # Populate reverse-index so subsequent ticks don't re-create.
            self._template_children.setdefault(template.id, set()).add(existing.id)
            self._child_to_template[existing.id] = template.id
            self._template_fire_attempts.pop(key, None)
            logger.info(
                f"Fire dedup template={template.identifier} slot={slot} "
                f"child={existing.identifier or existing.id} "
                f"(duplicate-sibling recovery)"
            )
            return

        # --- Step 1.5: overlap policy = cancel_previous (R8) ------------
        # Must run AFTER the duplicate-sibling check (so crash recovery on
        # the same slot doesn't trigger a redundant cancel) and BEFORE the
        # pending watermark (so cancel-prep failure doesn't leave a
        # pending that nobody will resolve).
        schedule_cfg = self._resolve_schedule_config(template)
        if (
            schedule_cfg is not None
            and schedule_cfg.overlap_policy == "cancel_previous"
            and active_siblings
        ):
            # Refuse-fire-on-config-absence: if the Canceled state name
            # isn't configured, downgrade this decision to skip_overlap
            # and move the template to Error. Prevents silent double-fires
            # when the config invariant is violated.
            canceled_state_name = (
                self.cfg.linear_states.canceled or ""
            ).strip()
            if not canceled_state_name:
                logger.error(
                    f"cancel_previous: canceled state missing in config — "
                    f"downgrading to skip_overlap for "
                    f"template={template.identifier} slot={slot}"
                )
                await self._post_skip_watermark(
                    template,
                    scheduler.FireDecision(
                        template_id=template.id,
                        slot=slot,
                        action="skip_overlap",
                        reason="cancel_previous_config_missing_canceled_state",
                    ),
                )
                await self._move_template_to_error(
                    template,
                    reason="canceled_state_missing",
                    details=(
                        "overlap_policy: cancel_previous requires "
                        "linear_states.canceled but it is not set"
                    ),
                )
                return

            # Cancel each in-flight sibling. Failures are logged but do
            # NOT block the new fire — per plan, a visible orphan is
            # preferable to a silent double-child.
            for sibling in active_siblings:
                try:
                    await self._cancel_child_for_overlap(
                        child_id=sibling.id,
                        child_identifier=sibling.identifier or sibling.id,
                        template_id=template.id,
                        template_identifier=template.identifier,
                        triggering_slot=slot,
                        canceled_state_name=canceled_state_name,
                    )
                except Exception as e:
                    logger.error(
                        f"cancel_previous: unexpected exception canceling "
                        f"sibling={sibling.identifier or sibling.id} for "
                        f"template={template.identifier} slot={slot}: {e}",
                        exc_info=True,
                    )
                    # Continue — still attempt remaining siblings + new fire.

        step = decide_materialize_step(
            decision, active_siblings, attempts_before
        )

        # --- Step 6 fast path: over retry budget -----------------------
        if step.kind == "fail_permanent":
            seq = self._next_seq(template.id)
            body = make_fired_comment(
                template_id=template.identifier,
                slot=slot,
                status="failed_permanent",
                reason="attempts_exceeded",
                attempt=attempts_before,
                seq=seq,
            )
            try:
                await client.post_comment(template.id, body)
            except Exception as e:
                logger.warning(
                    f"Failed to post failed_permanent watermark for "
                    f"{template.identifier}: {e}"
                )
            self._template_fire_attempts.pop(key, None)
            await self._move_template_to_error(
                template,
                reason="fire_failed_permanent",
                details=(
                    f"slot {slot} exhausted {MAX_FIRE_ATTEMPTS} fire "
                    f"attempts"
                ),
            )
            return

        # --- Step 2: post pending watermark -----------------------------
        attempt_n = step.attempt_number
        pending_seq = self._next_seq(template.id)
        pending_body = make_fired_comment(
            template_id=template.identifier,
            slot=slot,
            status="pending",
            attempt=attempt_n,
            seq=pending_seq,
        )
        try:
            await client.post_comment(template.id, pending_body)
        except Exception as e:
            logger.warning(
                f"Failed to post pending watermark for "
                f"{template.identifier} slot={slot}: {e}; next tick will "
                f"retry from step 1"
            )
            return

        # --- Step 3: create child ---------------------------------------
        schedule_cfg = self._resolve_schedule_config(template)
        schedule_name = schedule_cfg.name if schedule_cfg else None

        # Pre-resolve labels. If any label isn't found we skip it (don't
        # auto-create) and log a warning — operator is responsible for
        # creating schedule / slot labels up front.
        desired_labels: list[str] = []
        if schedule_name:
            desired_labels.append(workflow_label_name(schedule_name))
        desired_labels.append(slot_label_name(slot))

        label_ids: list[str] = []
        try:
            resolved = await client.resolve_label_ids(
                template.team_id, desired_labels,
            )
        except Exception as e:
            logger.warning(
                f"resolve_label_ids failed for {template.identifier}: {e}; "
                f"proceeding without label IDs"
            )
            resolved = {}

        for name in desired_labels:
            if name in resolved:
                label_ids.append(resolved[name])
            else:
                logger.warning(
                    f"Label {name!r} not found in team {template.team_id!r}; "
                    f"child for slot {slot} will not carry it. Create the "
                    f"label in Linear to enable duplicate-sibling detection."
                )

        title = build_child_title(template.title, slot)
        description = build_child_description(
            template_identifier=template.identifier,
            template_url=template.url,
            slot=slot,
            cron_expr=template.cron_expr,
            schedule_name=schedule_name,
            is_trigger=decision.is_trigger_now,
        )

        team_id = template.team_id or ""
        created: Issue | None = None
        error_reason: str | None = None
        try:
            if not team_id:
                error_reason = "missing_team_id"
            else:
                created = await client.create_child_issue(
                    parent_id=template.id,
                    team_id=team_id,
                    title=title,
                    description=description,
                    label_ids=label_ids or None,
                )
                if created is None:
                    error_reason = "create_rejected"
        except Exception as e:
            error_reason = f"create_exception:{type(e).__name__}"
            logger.warning(
                f"create_child_issue raised for {template.identifier} "
                f"slot={slot}: {e}"
            )

        # --- Step 4: success ------------------------------------------
        if created is not None and error_reason is None:
            success_seq = self._next_seq(template.id)
            success_body = make_fired_comment(
                template_id=template.identifier,
                slot=slot,
                status="child",
                child_id=created.identifier or created.id,
                seq=success_seq,
            )
            try:
                await client.post_comment(template.id, success_body)
            except Exception as e:
                logger.warning(
                    f"Failed to post success watermark for "
                    f"{template.identifier} slot={slot}: {e}; next tick's "
                    f"duplicate check will self-heal"
                )
            # Register the child for reverse-index + in-flight counting.
            self._template_children.setdefault(template.id, set()).add(created.id)
            self._child_to_template[created.id] = template.id
            self._template_fire_attempts.pop(key, None)
            self._template_last_fired[template.id] = datetime.now(timezone.utc)
            logger.info(
                f"Fire success template={template.identifier} slot={slot} "
                f"child={created.identifier or created.id} attempt={attempt_n}"
            )
            return

        # --- Steps 5/6: failure ---------------------------------------
        new_attempts = attempts_before + 1
        self._template_fire_attempts[key] = new_attempts

        if new_attempts >= MAX_FIRE_ATTEMPTS:
            # Permanent failure — post failed_permanent + Error transition.
            fail_seq = self._next_seq(template.id)
            fail_body = make_fired_comment(
                template_id=template.identifier,
                slot=slot,
                status="failed_permanent",
                reason=error_reason or "unknown",
                attempt=new_attempts,
                seq=fail_seq,
            )
            try:
                await client.post_comment(template.id, fail_body)
            except Exception as e:
                logger.warning(
                    f"Failed to post failed_permanent watermark for "
                    f"{template.identifier}: {e}"
                )
            self._template_fire_attempts.pop(key, None)
            await self._move_template_to_error(
                template,
                reason="fire_failed_permanent",
                details=(
                    f"slot {slot} exhausted {MAX_FIRE_ATTEMPTS} fire "
                    f"attempts (last reason: {error_reason})"
                ),
            )
            logger.error(
                f"Fire failed_permanent template={template.identifier} "
                f"slot={slot} attempts={new_attempts} reason={error_reason}"
            )
            return

        # Transient failure — post failed watermark, keep attempt counter.
        fail_seq = self._next_seq(template.id)
        fail_body = make_fired_comment(
            template_id=template.identifier,
            slot=slot,
            status="failed",
            reason=error_reason or "unknown",
            attempt=new_attempts,
            seq=fail_seq,
        )
        try:
            await client.post_comment(template.id, fail_body)
        except Exception as e:
            logger.warning(
                f"Failed to post failed watermark for "
                f"{template.identifier}: {e}"
            )
        logger.warning(
            f"Fire failed template={template.identifier} slot={slot} "
            f"attempt={new_attempts} reason={error_reason}"
        )

    async def _move_template_to_error(
        self,
        template: Issue,
        *,
        reason: str,
        details: str | None = None,
    ) -> None:
        """Move a template to the Error Linear state + post an error comment.

        Idempotent (R18): inspects the last ``stokowski:schedule_error``
        comment on the template. Only posts a NEW comment when the latest
        payload's ``reason`` differs from this call's reason. Prevents the
        per-tick comment-spam pathology when a template stays stuck on the
        same invalid cron for hours.

        Comment-write failure fallback: if ``post_comment`` raises (e.g.
        Linear 403 / rate-limit), a structured ERROR log line is written
        so operators get *something* even if Linear is flaking. The state
        move via ``update_issue_state`` is still attempted regardless —
        the two mutations are independent.

        Always stamps ``_template_error_since[template.id]`` with "now" on
        the first entry into error state; subsequent calls with the same
        reason preserve the original timestamp so the dashboard's
        "Error > 24h" dwell counter is accurate.
        """
        try:
            client = self._ensure_linear_client()
            # Idempotency check — fetch latest schedule_error comment and
            # compare reason. Fetch failures fall through to post the new
            # comment (safer to risk a duplicate than to skip a real error).
            existing_reason: str | None = None
            try:
                comments = await client.fetch_comments(template.id)
                latest = parse_latest_schedule_error(comments or [])
                if latest is not None:
                    existing_reason = latest.get("reason")
            except Exception as e:
                logger.debug(
                    f"Idempotency check for schedule_error failed "
                    f"(template={template.identifier}): {e}; posting anyway"
                )

            same_reason = (existing_reason == reason)
            if not same_reason:
                body = make_schedule_error_comment(
                    template_id=template.identifier,
                    reason=reason,
                    details=details,
                )
                try:
                    await client.post_comment(template.id, body)
                except Exception as e:
                    # Structured fallback log line — operator's last line of
                    # visibility when Linear is down. Not a warning: this
                    # is the error surface, so log at ERROR level.
                    logger.error(
                        f"schedule_error_comment_write_failed "
                        f"template={template.identifier} reason={reason} "
                        f"details={details!r} error={e}"
                    )

            # Mutation 2 (state move) is attempted regardless of mutation 1.
            # Even if the comment succeeded last tick, the state may have
            # been nudged out of Error by a human — moving it back is
            # cheap and idempotent from Linear's perspective.
            error_state = self.cfg.linear_states.schedule_error
            try:
                await client.update_issue_state(template.id, error_state)
            except Exception as e:
                logger.warning(
                    f"Failed to move {template.identifier} to "
                    f"{error_state!r}: {e}"
                )

            # Only stamp the error-since timestamp on FIRST entry or when
            # reason changed. Preserves dwell-time accuracy for the
            # dashboard's "Error > 24h" metric.
            if template.id not in self._template_error_since or not same_reason:
                self._template_error_since[template.id] = datetime.now(timezone.utc)

            # Log at ERROR on first entry / reason change; DEBUG on repeats
            # (operator already saw the first one, no need to spam stderr).
            if not same_reason:
                logger.error(
                    f"Template moved to Error template={template.identifier} "
                    f"reason={reason} details={details!r}"
                )
            else:
                logger.debug(
                    f"Template still in Error template={template.identifier} "
                    f"reason={reason} (idempotent, no new comment)"
                )
        except Exception as e:
            logger.error(
                f"_move_template_to_error failed for "
                f"{template.identifier}: {e}",
                exc_info=True,
            )

    def _clear_error_state_on_recovery(self) -> None:
        """Drop ``_template_error_since`` entries for templates back on track.

        R18: when an operator fixes an invalid cron and moves the template
        back to the Scheduled state, we need to clear the dwell-time
        counter so the dashboard's "Error > 24h" metric reflects reality.

        Called from ``_evaluate_schedules`` before the per-template loop.
        Purely a state-machine cleanup — no network I/O.
        """
        if not self._template_error_since:
            return
        error_state_lower = (self.cfg.linear_states.schedule_error or "").strip().lower()
        for tmpl_id in list(self._template_error_since):
            tmpl = self._template_snapshots.get(tmpl_id)
            if tmpl is None:
                # Template vanished from Linear — _cleanup_template_state
                # will handle the removal; leave the entry alone until then.
                continue
            current = (tmpl.state or "").strip().lower()
            if current != error_state_lower:
                self._template_error_since.pop(tmpl_id, None)

    async def _retention_sweep(self) -> None:
        """R13: budget-bounded archive of old terminal children.

        Iterates known templates, fetches their non-archived children, and
        archives those whose ``updated_at`` is older than the template's
        ``retention_days``. The sweep is bounded by
        ``RETENTION_BUDGET_PER_TICK`` archive mutations across ALL
        templates combined — a large backlog from a newly-adopted
        retention policy won't block the orchestrator loop.

        Ordering:
          1. Per template, candidates are sorted oldest-first
             (``updated_at`` ascending).
          2. Templates are processed in insertion order of
             ``_template_snapshots``. This is not strictly round-robin —
             a template with many very-old children can monopolise a
             single tick's budget. Acceptable: next tick resumes with
             whatever's left.

        Failure handling:
          * Transient archive failure → log + retry on the next sweep.
          * Same child fails N consecutive times →
            ``_retention_poison_pill_counts[child_id] >=
            RETENTION_POISON_PILL_THRESHOLD`` engages and the child is
            skipped in future sweeps (dashboard surfaces as "archive
            failing" via Unit 13).
          * Successful archive resets the poison-pill counter for that
            child.

        Backlog indicator:
          ``self._retention_backlog_detected`` is set True whenever the
          budget is exhausted, so Unit 13 can surface a "retention backlog
          large — outside tested regime" banner.

        Best-effort: never raises. The caller (``_tick``) wraps in a
        try/except anyway.
        """
        if not self.cfg.schedules:
            # No schedules configured → no templates → no children to sweep.
            self._retention_backlog_detected = False
            return
        if not self._template_snapshots:
            self._retention_backlog_detected = False
            return

        try:
            client = self._ensure_linear_client()
        except Exception as e:
            logger.warning(f"Retention sweep: Linear client unavailable: {e}")
            return

        now = datetime.now(timezone.utc)
        budget = RETENTION_BUDGET_PER_TICK
        backlog_detected = False

        for template_id, template in list(self._template_snapshots.items()):
            if budget <= 0:
                # Leftover work exists — signal to dashboard.
                backlog_detected = True
                break

            schedule_cfg = self._resolve_schedule_config(template)
            if schedule_cfg is None:
                continue

            retention_days = schedule_cfg.retention_days
            if retention_days < 1:
                continue

            try:
                children = await client.fetch_template_children(
                    template_id, include_archived=False
                )
            except Exception as e:
                logger.warning(
                    f"Retention sweep: failed to fetch children for "
                    f"template={template.identifier}: {e}"
                )
                continue

            candidates = select_retention_candidates(
                children, now, retention_days,
            )
            # Skip poison-pilled children — they've failed repeatedly and
            # would just re-burn budget every tick.
            candidates = [
                c for c in candidates
                if self._retention_poison_pill_counts.get(c.id, 0)
                < RETENTION_POISON_PILL_THRESHOLD
            ]

            if not candidates:
                continue

            # If this single template has more candidates than budget, we
            # know there's a backlog even if nothing remains for other
            # templates. The outer loop check catches the cross-template
            # case; this catches the intra-template case.
            if len(candidates) > budget:
                backlog_detected = True

            for child in candidates:
                if budget <= 0:
                    backlog_detected = True
                    break
                ok = False
                try:
                    ok = await client.archive_issue(child.id)
                except Exception as e:
                    logger.warning(
                        f"Retention sweep: archive raised for "
                        f"child={child.identifier} "
                        f"template={template.identifier}: {e}"
                    )
                    ok = False

                budget -= 1
                if ok:
                    # Success: clear any previous failure state for this
                    # child; record wall-clock for dashboard.
                    self._retention_poison_pill_counts.pop(child.id, None)
                    self._retention_last_archive_at[child.id] = now
                else:
                    prior = self._retention_poison_pill_counts.get(child.id, 0)
                    new_count = prior + 1
                    self._retention_poison_pill_counts[child.id] = new_count
                    if new_count == RETENTION_POISON_PILL_THRESHOLD:
                        # Loud once, then silent on future sweeps.
                        logger.warning(
                            f"Retention sweep: poison pill engaged for "
                            f"child={child.identifier} "
                            f"template={template.identifier} "
                            f"after {new_count} consecutive failures; "
                            f"skipping in future sweeps"
                        )

        self._retention_backlog_detected = backlog_detected

    async def _tick(self):
        """Single poll tick: reconcile, validate, fetch, dispatch.

        Multi-project: iterate each loaded project, fetch with its own
        LinearClient, and stamp ``_issue_project[issue.id]`` before any
        downstream per-issue config lookup runs.
        """
        # Reload workflow (supports hot-reload) — independent per-file.
        errors_by_slug = self._load_all_workflows()
        if errors_by_slug:
            for slug, errs in errors_by_slug.items():
                logger.warning(f"Config invalid ({slug}): {errs}")

        # Fetch templates BEFORE reconcile so hard-delete detection (Unit 8)
        # sees the fresh set. See the plan's "Key Technical Decisions".
        await self._fetch_templates()

        # Part 1: Reconcile running issues
        await self._reconcile()

        # Handle gate responses
        await self._handle_gate_responses()

        # Part 2: If every project has errored, skip dispatch entirely.
        if not self.configs:
            logger.warning("No healthy projects loaded, skipping dispatch")
            return

        # Part 2.5: Evaluate schedules. Children materialized here are
        # picked up by this same tick's candidate fetch below — avoids a
        # full poll interval of latency between fire and dispatch.
        await self._safe_evaluate_schedules()

        # Part 3: Fetch candidates — per project, with its own client.
        all_candidates: list[Issue] = []
        for slug, parsed in list(self.configs.items()):
            pcfg = parsed.config
            try:
                client = self._linear_client_for(slug)
                candidates = await client.fetch_candidate_issues(
                    pcfg.tracker.project_slug,
                    pcfg.active_linear_states(),
                )
            except Exception as e:
                logger.error(f"Failed to fetch candidates for {slug}: {e}")
                continue
            for issue in candidates:
                # Bind every returned issue to its project BEFORE any
                # downstream lookup that may call _cfg_for_issue.
                self._issue_project[issue.id] = slug
            all_candidates.extend(candidates)

        candidates = all_candidates

        # Snapshot prior-tick labels BEFORE updating _last_issues. See COR-001.
        for issue in candidates:
            prior = self._last_issues.get(issue.id)
            if prior is not None:
                self._prev_issue_labels[issue.id] = sorted(
                    l.lower() for l in prior.labels
                )
            else:
                self._prev_issue_labels.pop(issue.id, None)

        # Cache issues for retry lookup
        for issue in candidates:
            self._last_issues[issue.id] = issue

        # Part 4: Sort by priority (global sort — projects interleave)
        candidates.sort(
            key=lambda i: (
                i.priority if i.priority is not None else 999,
                i.created_at or datetime.min.replace(tzinfo=timezone.utc),
                i.identifier,
            )
        )

        # Resolve state for new issues before dispatch
        for issue in candidates:
            if issue.id not in self._issue_current_state and issue.id not in self.running:
                try:
                    await self._resolve_current_state(issue)
                except Exception as e:
                    logger.warning(f"Failed to resolve state for {issue.identifier}: {e}")

        # Rejection pre-pass
        await self._process_rejections(candidates)

        # Part 5: Dispatch — shared global budget from the primary config.
        primary_cfg = self._primary_cfg()
        available_slots = max(
            primary_cfg.agent.max_concurrent_agents - len(self.running), 0
        )

        # Track starved projects for the end-of-dispatch WARN log.
        any_dispatch = False

        for issue in candidates:
            if available_slots <= 0:
                break
            if not self._is_eligible(issue):
                continue

            # Per-state concurrency check — uses the issue's own project cfg
            # (state names are per-project; a limit from project A cannot
            # apply to project B's states).
            issue_cfg = self._cfg_for_issue(issue.id)
            internal_state = self._issue_current_state.get(issue.id, "")
            state_limit = issue_cfg.agent.max_concurrent_agents_by_state.get(
                internal_state
            )
            if state_limit is not None:
                state_count = sum(
                    1
                    for r in self.running.values()
                    if r.state_name == internal_state
                )
                if state_count >= state_limit:
                    continue

            self._dispatch(issue)
            any_dispatch = True
            available_slots -= 1

        # Starvation signal: if the budget is exhausted AND some project has
        # eligible issues but zero running, operators should see it.
        if available_slots == 0 and self.running:
            running_slugs = {
                self._issue_project.get(iid)
                for iid in self.running
                if iid in self._issue_project
            }
            for slug in self.configs.keys():
                if slug in running_slugs:
                    continue
                starved = [
                    c for c in candidates
                    if self._issue_project.get(c.id) == slug
                    and self._is_eligible(c)
                ]
                if starved:
                    logger.warning(
                        f"Dispatch budget exhausted: project={slug} has "
                        f"{len(starved)} eligible but 0 running issues"
                    )

        # Retention sweep. Runs LAST so the dispatch pass is never
        # delayed by archive latency. Awaited (not fire-and-forget) so
        # exceptions surface to the caller's normal per-tick error path
        # and sweep timing is deterministic across ticks.
        try:
            await self._retention_sweep()
        except Exception as e:
            logger.error(f"Retention sweep failed: {e}", exc_info=True)

    async def _resolve_repo_for_coldstart(
        self,
        issue: Issue,
        tracking: dict | None,
        comments: list[dict],
    ) -> None:
        """Restore _issue_repo after a cold start, with migration handling.

        If the cache is already populated for this issue (fresh dispatch in
        this process), nothing to do.

        If tracking has a ``repo`` field (post-multi-repo tracking comment),
        use it — but verify the repo still exists in config. A hot-reload
        could have removed it; fall through to label resolution then.

        If tracking has no ``repo`` field (pre-multi-repo tracking comment,
        or the very first dispatch in this process), fall back to resolving
        via labels. If the result is the synthetic ``_default`` repo AND
        this is the first time we've processed this issue in the current
        process, post a one-time ``stokowski:migrated`` comment so the
        cold-start decision is recorded.
        """
        if issue.id in self._issue_repo:
            return

        # Defensive read — payload.get() never direct-subscript
        cfg = self._cfg_for_issue_or_primary(issue.id)
        tracked_repo_name: str | None = None
        if tracking is not None:
            tracked_repo_name = tracking.get("repo")

        if tracked_repo_name is not None:
            repo = cfg.repos.get(tracked_repo_name)
            if repo is not None:
                self._issue_repo[issue.id] = repo.name
                return
            # Tracked repo no longer in config — fall through to labels.
            logger.info(
                f"Cold-start: tracked repo '{tracked_repo_name}' no longer "
                f"in config for {issue.identifier}; re-resolving from labels"
            )

        # Resolve from labels
        try:
            resolved = cfg.resolve_repo(issue)
        except ValueError:
            # Shouldn't happen with a validated config — log and bail
            logger.warning(
                f"Cold-start: resolve_repo failed for {issue.identifier}; "
                f"skipping repo cache population"
            )
            return

        self._issue_repo[issue.id] = resolved.name

        # Handle pre-multi-repo tracking (tracking exists but has no repo
        # field). Two post-migration actions, in priority order:
        #   (1) Post a one-time stokowski:migrated comment so the recovery
        #       decision is auditable on the ticket thread.
        #   (2) Log a warning if the issue also has a repo:* label that
        #       didn't match any configured repo (could indicate config
        #       drift since the issue was last dispatched).
        #
        # KP-06: the previous structure ran these as two independent blocks
        # with the same guard, which made the warning in (2) dead code on
        # the (1)-success path (because (1) added issue.id to
        # _migrated_issues, invalidating the "not in _migrated_issues"
        # guard in (2)). Restructured as an if/elif so both paths are
        # reachable: warning fires when the migration comment post fails.
        pre_migration = (
            tracking is not None
            and tracking.get("repo") is None
            and resolved.name == "_default"
        )
        if pre_migration and issue.id not in self._migrated_issues:
            try:
                client = self._ensure_linear_client()
                await client.post_comment(
                    issue.id, make_migrated_comment(resolved.name),
                )
                self._migrated_issues.add(issue.id)
            except Exception as e:
                logger.debug(
                    f"Failed to post migrated comment for "
                    f"{issue.identifier}: {e}"
                )
                # Fall through to the operator-facing warning below so at
                # least the log has a signal that we hit the migration
                # path. The warning is cheaper than the comment and costs
                # nothing on repeat ticks.

        if pre_migration:
            has_repo_label = any(
                l.lower().startswith("repo:") for l in (issue.labels or [])
            )
            if has_repo_label and issue.id not in self._migrated_issues:
                logger.warning(
                    f"Cold-start: {issue.identifier} has repo:* label(s) "
                    f"but fell back to _default — labels: {issue.labels}"
                )

    async def _process_rejections(self, issues: list[Issue]) -> None:
        """Async pre-pass: enforce R10 single-repo cap for each candidate.

        For each issue:
        1. Count ``repo:*`` labels on ``issue.labels``.
        2. If > 1 AND the issue was previously rejected with a DIFFERENT
           label set (labels were edited), discard the prior marker so we
           can re-evaluate against the new labels on this tick.
        3. If > 1 AND no marker exists, fetch the comment thread, check
           for an existing rejection sentinel via ``has_pending_rejection``
           against the current sorted label set, post a new sentinel iff
           none exists, then add ``issue.id`` to ``_rejected_issues``.
        4. If <= 1, ensure ``issue.id`` is NOT in ``_rejected_issues`` —
           operator fixed the labels, dispatch can proceed.

        Failures to fetch comments are logged but do not populate the set
        (err on the side of allowing dispatch rather than silently
        stalling the ticket).
        """
        if not issues:
            return

        for issue in issues:
            # Each issue uses its own project's Linear client.
            client = self._client_for_issue(issue.id)
            current_labels = [l.lower() for l in (issue.labels or [])]
            repo_labels = [l for l in current_labels if l.startswith("repo:")]

            # Clear stale markers when labels changed since the prior tick.
            # _prev_issue_labels is captured at the top of _tick BEFORE
            # _last_issues is updated, so the comparison is against the
            # PREVIOUS tick's labels, not the current tick's (COR-001).
            prior_labels = self._prev_issue_labels.get(issue.id)
            if (
                issue.id in self._rejected_issues
                and prior_labels is not None
                and prior_labels != sorted(current_labels)
            ):
                self._rejected_issues.discard(issue.id)
                self._rejection_fetch_pending.discard(issue.id)

            # Re-evaluate pessimistic rejections (fetch failures from prior
            # ticks) regardless of label change — the Linear outage may have
            # cleared and we should re-attempt the dedup check.
            if issue.id in self._rejection_fetch_pending:
                self._rejected_issues.discard(issue.id)
                self._rejection_fetch_pending.discard(issue.id)

            if len(repo_labels) > 1:
                if issue.id in self._rejected_issues:
                    # Already handled on an earlier tick; nothing to do.
                    continue

                # Check existing comments to avoid duplicate rejection posts
                try:
                    comments = await client.fetch_comments(issue.id)
                except Exception as e:
                    # Fail-closed (ADV-003): without knowing whether a prior
                    # rejection comment exists, dispatching a dual-labeled
                    # ticket would commit an arbitrary first-wins repo
                    # routing to the tracking thread — worse than a
                    # temporary stall. Mark rejected AND flag for retry so
                    # the next tick re-attempts the fetch regardless of
                    # whether labels changed.
                    logger.error(
                        f"Rejection pre-pass: failed to fetch comments for "
                        f"{issue.identifier}: {e}. Failing closed — will "
                        f"retry on next tick."
                    )
                    self._rejected_issues.add(issue.id)
                    self._rejection_fetch_pending.add(issue.id)
                    continue

                if not has_pending_rejection(comments, current_labels):
                    # Detect triage origin: if the most recent state-tracking
                    # comment's workflow field is a triage workflow, attribute
                    # the conflict to triage rather than human labeling.
                    tracking = parse_latest_tracking(comments)
                    reason = "multi_repo"
                    if tracking and tracking.get("type") == "state":
                        wf_name = tracking.get("workflow")
                        if wf_name:
                            issue_cfg_rej = self._cfg_for_issue_or_primary(issue.id)
                            wf = issue_cfg_rej.get_workflow(wf_name)
                            if wf and wf.triage:
                                reason = "triage_multi_repo"
                    comment_body = make_rejection_comment(
                        current_labels, reason=reason
                    )
                    try:
                        await client.post_comment(issue.id, comment_body)
                        logger.info(
                            f"R10 rejection posted for {issue.identifier} "
                            f"(labels={repo_labels}, reason={reason})"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to post rejection comment for "
                            f"{issue.identifier}: {e}"
                        )
                        # Do NOT mark as rejected if the post failed —
                        # next tick will retry.
                        continue

                self._rejected_issues.add(issue.id)
            else:
                # <= 1 repo label: dispatch may proceed. Clear any stale
                # marker left over from a prior dual-label state.
                self._rejected_issues.discard(issue.id)

    def _is_eligible(self, issue: Issue) -> bool:
        """Check if an issue is eligible for dispatch."""
        # Templates are never dispatched — they are configuration rows, not
        # work items. This is defense-in-depth (R4): templates normally live
        # in reserved states outside active_linear_states() so they don't
        # reach this check, but a mis-mapped config must not leak one into
        # the dispatch path.
        for label in (issue.labels or []):
            if label.lower().startswith("schedule:"):
                return False

        if not issue.id or not issue.identifier or not issue.title or not issue.state:
            return False

        # R10: more than one repo:* label → ineligible. The async pre-pass
        # (see _process_rejections) has already done the Linear work of
        # posting a dedup'd rejection comment; we just observe the set here.
        if issue.id in self._rejected_issues:
            return False

        # Config-error block: hook template rendering failed on a prior
        # dispatch. Retrying won't fix a config typo; _load_workflow clears
        # this set when the operator reloads a valid workflow.yaml.
        if issue.id in self._config_blocked:
            return False

        # Multi-project: per-issue state names (a ticket from project B must
        # pass eligibility using project B's linear_states names).
        cfg = self._cfg_for_issue_or_primary(issue.id)
        state_lower = issue.state.strip().lower()
        active_lower = [s.strip().lower() for s in cfg.active_linear_states()]
        terminal_lower = [s.strip().lower() for s in cfg.terminal_linear_states()]

        if state_lower not in active_lower:
            return False
        if state_lower in terminal_lower:
            return False
        if issue.id in self.running:
            return False
        if issue.id in self.claimed:
            return False

        # Blocker check for Todo
        if state_lower == "todo":
            for blocker in issue.blocked_by:
                if blocker.state and blocker.state.strip().lower() not in terminal_lower:
                    return False

        return True

    def _dispatch(self, issue: Issue, attempt_num: int | None = None):
        """Dispatch a worker for an issue."""
        self.claimed.add(issue.id)

        issue_cfg = self._cfg_for_issue_or_primary(issue.id)
        state_name = self._issue_current_state.get(issue.id)
        if not state_name:
            state_name = self._get_issue_workflow_config(issue.id).entry_state or issue_cfg.entry_state

        # If at a gate, enter it instead of dispatching a worker
        state_cfg = issue_cfg.states.get(state_name) if state_name else None
        if state_cfg and state_cfg.type == "gate":
            asyncio.create_task(self._safe_enter_gate(issue, state_name))
            return

        attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=attempt_num,
            state_name=state_name,
        )

        # Session handling
        use_fresh_session = False
        if state_cfg and state_cfg.session == "fresh":
            use_fresh_session = True

        if not use_fresh_session:
            if issue.id in self.running:
                old = self.running[issue.id]
                if old.session_id:
                    attempt.session_id = old.session_id
            elif issue.id in self._last_session_ids:
                attempt.session_id = self._last_session_ids[issue.id]

        self.running[issue.id] = attempt
        task = asyncio.create_task(self._run_worker(issue, attempt))
        self._tasks[issue.id] = task

        runner = state_cfg.runner if state_cfg else "claude"
        logger.info(
            f"Dispatched issue={issue.identifier} "
            f"state={issue.state} "
            f"machine_state={state_name or 'entry'} "
            f"runner={runner} "
            f"session={'fresh' if use_fresh_session else 'inherit'} "
            f"attempt={attempt_num}"
        )

    async def _run_worker(self, issue: Issue, attempt: RunAttempt):
        """Worker coroutine: prepare workspace, run agent turns.

        Hot-reload race (ADV-005): ``self.cfg`` is mutated by
        ``_load_all_workflows`` at every tick. If a concurrent tick runs
        while this coroutine is awaiting, subsequent ``self.cfg`` reads
        inside this worker would see a fresh (possibly different) config.

        Multi-project: ``cfg`` is pinned to the issue's OWN project config
        (resolved via ``_cfg_for_issue_or_primary``). Hot-reload of a
        DIFFERENT project's file does not affect this worker.
        """
        cfg = self._cfg_for_issue_or_primary(issue.id)
        try:
            # Resolve state if not set
            if not attempt.state_name:
                state_name, run = await self._resolve_current_state(issue)
                attempt.state_name = state_name
                state_cfg = cfg.states.get(state_name)
                if state_cfg and state_cfg.type == "gate":
                    # Issue should be at a gate, not running
                    await self._enter_gate(issue, state_name)
                    return

            state_name = attempt.state_name
            state_cfg = cfg.states.get(state_name) if state_name else None

            claude_cfg = cfg.claude
            hooks_cfg = cfg.hooks
            runner_type = "claude"

            if state_cfg:
                claude_cfg, hooks_cfg = merge_state_config(
                    state_cfg, cfg.claude, cfg.hooks
                )
                runner_type = state_cfg.runner

            ws_root = cfg.workspace.resolved_root()
            # Resolve repo using the pinned cfg snapshot (ADV-005). A
            # concurrent _load_workflow during our await could have swapped
            # self.cfg; resolving via `cfg.resolve_repo` guarantees the
            # repo corresponds to the config we started this dispatch
            # against. _issue_repo cache is still updated so subsequent
            # ticks see the resolution.
            try:
                repo = cfg.resolve_repo(issue)
                self._issue_repo[issue.id] = repo.name
            except ValueError as e:
                # No default repo configured — config got corrupted by a
                # hot-reload between dispatch and here, or the operator
                # removed the default. Treat as config_error.
                msg = (
                    f"Repo resolution failed for {issue.identifier}: {e}. "
                    f"No matching `repo:*` label and no default repo in "
                    f"the current config."
                )
                logger.error(msg)
                self._fire_and_forget(self._post_hook_error_comment(issue.id, msg))
                attempt.status = "config_error"
                attempt.error = str(e)
                self._on_worker_exit(issue, attempt)
                return
            docker_image = ""
            if cfg.docker.enabled:
                docker_image = _resolve_docker_image(
                    state_cfg, repo, cfg.docker.default_image,
                )
            # Render root hooks with repo metadata when the config has an
            # explicit repos: section. Legacy configs (repos_synthesized)
            # bypass rendering — preserves R19 backward compat for hook
            # bodies containing literal {}/${ shell syntax.
            try:
                rendered_hooks = render_hooks_for_dispatch(
                    cfg.hooks, repo, cfg.repos_synthesized
                )
            except (UndefinedError, TemplateSyntaxError) as render_err:
                # Config-level problem in a hook template:
                # - UndefinedError: a variable reference like {{ repo.clne_url }} (typo)
                # - TemplateSyntaxError: a `{{ ` / `{% ` that Jinja2 can't parse
                #   (common when migrating a legacy config with Jinja2-conflicting
                #   shell syntax from 1:1 to multi-repo mode).
                # Surface on Linear + mark status='config_error' so _on_worker_exit
                # routes to _config_blocked (no retry loop) instead of scheduling
                # exponential backoff against a broken template.
                if isinstance(render_err, UndefinedError):
                    hint = (
                        "This typically means a typo in a Jinja2 variable "
                        "reference (e.g. `{{ repo.clne_url }}` instead of "
                        "`{{ repo.clone_url }}`)."
                    )
                else:
                    hint = (
                        "Jinja2 could not parse the hook body. Common cause: "
                        "migrating a legacy 1:1 config with shell syntax like "
                        "`!f() {{ ...; }}; f` or literal `{{ `/`{% ` tokens to "
                        "multi-repo mode, which activates Jinja2 rendering. "
                        "Either escape the tokens (`{{ '{{' }}`) or rework the "
                        "hook body."
                    )
                msg = (
                    f"Hook template rendering failed for "
                    f"{issue.identifier} (repo={repo.name}): "
                    f"{type(render_err).__name__}: {render_err}. {hint}"
                )
                logger.error(msg)
                self._fire_and_forget(self._post_hook_error_comment(issue.id, msg))
                attempt.status = "config_error"
                attempt.error = f"hook template error: {render_err}"
                self._on_worker_exit(issue, attempt)
                return
            ws = await ensure_workspace(
                ws_root, issue.identifier, repo.name, rendered_hooks,
                docker_cfg=cfg.docker if cfg.docker.enabled else None,
                docker_image=docker_image,
            )
            attempt.workspace_path = str(ws.path)

            # Move issue from Todo to In Progress if needed
            todo_state = cfg.linear_states.todo
            if todo_state and issue.state.strip().lower() == todo_state.strip().lower():
                try:
                    client = self._client_for_issue(issue.id)
                    active_state = cfg.linear_states.active
                    moved = await client.update_issue_state(issue.id, active_state)
                    if moved:
                        issue.state = active_state
                        logger.info(
                            f"Moved {issue.identifier} from '{todo_state}' to '{active_state}'"
                        )
                    else:
                        logger.warning(
                            f"Failed to move {issue.identifier} from '{todo_state}' to '{active_state}' "
                            f"— Linear API returned failure"
                        )
                except Exception as e:
                    logger.warning(f"Failed to move {issue.identifier} to active: {e}")

            # Post state tracking comment (only for first dispatch of a state)
            if state_name:
                run = self._issue_state_runs.get(issue.id, 1)
                if run == 1 and (attempt.attempt is None or attempt.attempt == 0):
                    wf = self._get_issue_workflow_config(issue.id)
                    client = self._client_for_issue(issue.id)
                    comment = make_state_comment(
                        state=state_name,
                        run=run,
                        workflow=wf.name,
                        repo=repo.name,
                    )
                    await client.post_comment(issue.id, comment)

            # Run on_stage_enter hook if defined. Render over repo metadata
            # (same gate as root hooks — only when repos: section is explicit).
            if state_cfg and state_cfg.hooks and state_cfg.hooks.on_stage_enter:
                from .workspace import run_hook
                try:
                    stage_enter_script = (
                        state_cfg.hooks.on_stage_enter
                        if cfg.repos_synthesized
                        else render_hook_template(
                            state_cfg.hooks.on_stage_enter, repo
                        )
                    )
                except (UndefinedError, TemplateSyntaxError) as render_err:
                    msg = (
                        f"on_stage_enter hook template rendering failed for "
                        f"{issue.identifier} state={state_name} repo={repo.name}: "
                        f"{type(render_err).__name__}: {render_err}"
                    )
                    logger.error(msg)
                    self._fire_and_forget(
                        self._post_hook_error_comment(issue.id, msg)
                    )
                    attempt.status = "config_error"
                    attempt.error = f"hook template error: {render_err}"
                    self._on_worker_exit(issue, attempt)
                    return
                ok = await run_hook(
                    stage_enter_script,
                    ws.path,
                    (state_cfg.hooks.timeout_ms if state_cfg.hooks else cfg.hooks.timeout_ms),
                    f"on_stage_enter:{state_name}",
                    docker_cfg=cfg.docker if cfg.docker.enabled else None,
                    docker_image=docker_image,
                    workspace_key=ws.workspace_key,
                )
                if not ok:
                    attempt.status = "failed"
                    attempt.error = f"on_stage_enter hook failed for state {state_name}"
                    self._on_worker_exit(issue, attempt)
                    return

            prompt = await self._render_prompt_async(issue, attempt.attempt, state_name)

            # Build env vars for the agent subprocess using the pinned cfg.
            if cfg.docker.enabled:
                agent_env = cfg.docker_env()
            else:
                agent_env = cfg.agent_env()
            agent_env["STOKOWSKI_ISSUE_IDENTIFIER"] = issue.identifier
            # Multi-project: pass the owning project slug so hooks can branch
            # on it (R7).
            slug = self._issue_project.get(issue.id)
            if slug:
                agent_env["STOKOWSKI_LINEAR_PROJECT_SLUG"] = slug
            # Per-dispatch repo env vars (R16).
            agent_env["STOKOWSKI_REPO_NAME"] = repo.name
            if repo.clone_url:
                agent_env["STOKOWSKI_REPO_CLONE_URL"] = repo.clone_url
            # Triage workflow dispatch: inject the tenant-scoped repo list so
            # the triage agent can emit repo:<name> labels (R17, R18a).
            workflow_for_env = self._get_issue_workflow_config(issue.id)
            if workflow_for_env.triage:
                repo_list = [
                    {
                        "name": r.name,
                        "label": r.label or "",
                        "clone_url": r.clone_url or "",
                    }
                    for r in cfg.repos.values()
                    if r.name != "_default"
                ]
                agent_env["STOKOWSKI_REPOS_JSON"] = json.dumps(repo_list)

            # Build log path if logging enabled (per-project logging config).
            log_path = None
            if cfg.logging.enabled and cfg.logging.log_dir:
                workflow_dir = self._workflow_dir_for_issue(issue.id)
                log_dir = cfg.logging.resolved_log_dir(workflow_dir)
                issue_log_dir = log_dir / sanitize_key(issue.identifier)
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                ext = ".log" if runner_type == "codex" else ".ndjson"
                log_path = issue_log_dir / f"{timestamp}-turn-{attempt.turn_count + 1}{ext}"

            # State machine mode: single turn per dispatch. The state
            # machine handles continuation via _transition after each
            # turn completes — multi-turn loops would bypass gate
            # transitions and cause the agent to blow past stage
            # boundaries.
            if state_name and state_cfg:
                attempt = await run_turn(
                    runner_type=runner_type,
                    claude_cfg=claude_cfg,
                    hooks_cfg=hooks_cfg,
                    prompt=prompt,
                    workspace_path=ws.path,
                    issue=issue,
                    attempt=attempt,
                    on_event=self._on_agent_event,
                    on_pid=self._on_child_pid,
                    env=agent_env,
                    docker_cfg=cfg.docker,
                    workspace_key=ws.workspace_key,
                    docker_image=docker_image,
                    log_path=log_path,
                )
            else:
                # Legacy mode: multi-turn loop
                max_turns = claude_cfg.max_turns
                for turn in range(max_turns):
                    if turn > 0:
                        current_state = issue.state
                        try:
                            client = self._client_for_issue(issue.id)
                            states = await client.fetch_issue_states_by_ids([issue.id])
                            current_state = states.get(issue.id, issue.state)
                            state_lower = current_state.strip().lower()
                            active_lower = [
                                s.strip().lower() for s in cfg.active_linear_states()
                            ]
                            if state_lower not in active_lower:
                                logger.info(
                                    f"Issue {issue.identifier} no longer active "
                                    f"(state={current_state}), stopping"
                                )
                                break
                        except Exception as e:
                            logger.warning(f"State check failed, continuing: {e}")

                        prompt = (
                            f"Continue working on {issue.identifier}. "
                            f"The issue is still in '{current_state}' state. "
                            f"Check your progress and continue the task."
                        )

                    # Recalculate log path per turn in legacy mode
                    turn_log_path = None
                    if cfg.logging.enabled and cfg.logging.log_dir:
                        workflow_dir = self._workflow_dir_for_issue(issue.id)
                        log_dir = cfg.logging.resolved_log_dir(workflow_dir)
                        issue_log_dir = log_dir / sanitize_key(issue.identifier)
                        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                        ext = ".log" if runner_type == "codex" else ".ndjson"
                        turn_log_path = issue_log_dir / f"{timestamp}-turn-{attempt.turn_count + 1}{ext}"

                    attempt = await run_turn(
                        runner_type=runner_type,
                        claude_cfg=claude_cfg,
                        hooks_cfg=hooks_cfg,
                        prompt=prompt,
                        workspace_path=ws.path,
                        issue=issue,
                        attempt=attempt,
                        on_event=self._on_agent_event,
                        on_pid=self._on_child_pid,
                        env=agent_env,
                        docker_cfg=cfg.docker,
                        workspace_key=ws.workspace_key,
                        docker_image=docker_image,
                        log_path=turn_log_path,
                    )

                    if attempt.status != "succeeded":
                        break

            self._on_worker_exit(issue, attempt)

        except asyncio.CancelledError:
            logger.info(f"Worker cancelled issue={issue.identifier}")
            attempt.status = "canceled"
            self._on_worker_exit(issue, attempt)
        except Exception as e:
            logger.error(f"Worker error issue={issue.identifier}: {e}")
            attempt.status = "failed"
            attempt.error = str(e)
            self._on_worker_exit(issue, attempt)

    async def _render_prompt_async(
        self, issue: Issue, attempt_num: int | None, state_name: str | None = None
    ) -> str:
        """Render prompt using state machine prompt assembly (async — fetches comments)."""
        cfg = self._cfg_for_issue_or_primary(issue.id)
        if state_name and state_name in cfg.states:
            state_cfg = cfg.states[state_name]
            run = self._issue_state_runs.get(issue.id, 1)
            last_completed = self._last_completed_at.get(issue.id)
            last_run_at = last_completed.isoformat() if last_completed else None

            # Fetch comments via the issue's own project client.
            comments: list[dict] | None = None
            try:
                client = self._client_for_issue(issue.id)
                comments = await client.fetch_comments(issue.id)
            except Exception as e:
                logger.warning(f"Failed to fetch comments for prompt: {e}")

            state_cfg_for_rework = cfg.states.get(state_name)
            is_rework = (
                run > 1
                and (not state_cfg_for_rework or state_cfg_for_rework.session != "fresh")
            )

            workflow = self._get_issue_workflow_config(issue.id)
            wf_transitions = workflow.transitions.get(state_name)

            repo = self._get_issue_repo_config(issue.id)

            workflow_dir = str(self._workflow_dir_for_issue(issue.id))
            return assemble_prompt(
                cfg=cfg,
                workflow_dir=workflow_dir,
                issue=issue,
                state_name=state_name,
                state_cfg=state_cfg,
                run=run,
                is_rework=is_rework,
                attempt=attempt_num or 1,
                last_run_at=last_run_at,
                comments=comments,
                transitions=wf_transitions,
                repo=repo,
            )

        # Legacy fallback
        return self._render_prompt(issue, attempt_num, state_name)

    def _render_prompt(
        self, issue: Issue, attempt_num: int | None, state_name: str | None = None
    ) -> str:
        """Render the prompt template with issue context (legacy/sync fallback)."""
        cfg = self._cfg_for_issue_or_primary(issue.id)

        # State machine mode: call assemble_prompt without comments
        if state_name and state_name in cfg.states:
            state_cfg = cfg.states[state_name]
            run = self._issue_state_runs.get(issue.id, 1)
            last_completed = self._last_completed_at.get(issue.id)
            last_run_at = last_completed.isoformat() if last_completed else None

            workflow = self._get_issue_workflow_config(issue.id)
            wf_transitions = workflow.transitions.get(state_name)

            repo = self._get_issue_repo_config(issue.id)

            workflow_dir = str(self._workflow_dir_for_issue(issue.id))
            return assemble_prompt(
                cfg=cfg,
                workflow_dir=workflow_dir,
                issue=issue,
                state_name=state_name,
                state_cfg=state_cfg,
                run=run,
                is_rework=False,
                attempt=attempt_num or 1,
                last_run_at=last_run_at,
                comments=None,
                transitions=wf_transitions,
                repo=repo,
            )

        # Legacy mode: use workflow prompt_template with Jinja2
        # Resolve the ParsedConfig for legacy template access.
        slug = self._issue_project.get(issue.id)
        parsed = self.configs.get(slug) if slug else None
        if parsed is None:
            parsed = next(iter(self.configs.values())) if self.configs else self.workflow
        template_str = parsed.prompt_template if parsed else ""

        if not template_str:
            return f"You are working on an issue from Linear: {issue.identifier} - {issue.title}"

        last_completed = self._last_completed_at.get(issue.id)
        last_run_at = last_completed.isoformat() if last_completed else ""

        try:
            template = self._jinja.from_string(template_str)
            return template.render(
                issue={
                    "id": issue.id,
                    "identifier": issue.identifier,
                    "title": issue.title,
                    "description": issue.description or "",
                    "priority": issue.priority,
                    "state": issue.state,
                    "branch_name": issue.branch_name,
                    "url": issue.url,
                    "labels": issue.labels,
                    "blocked_by": [
                        {"id": b.id, "identifier": b.identifier, "state": b.state}
                        for b in issue.blocked_by
                    ],
                    "created_at": str(issue.created_at) if issue.created_at else "",
                    "updated_at": str(issue.updated_at) if issue.updated_at else "",
                },
                attempt=attempt_num,
                last_run_at=last_run_at,
                stage=state_name,
            )
        except TemplateSyntaxError as e:
            raise RuntimeError(f"Template syntax error: {e}")

    def _on_child_pid(self, pid: int, is_register: bool):
        """Track child claude process PIDs for cleanup on shutdown."""
        if is_register:
            self._child_pids.add(pid)
        else:
            self._child_pids.discard(pid)

    def _on_agent_event(self, identifier: str, event_type: str, event: dict):
        """Callback for agent events."""
        logger.debug(f"Agent event issue={identifier} type={event_type}")

    def _on_worker_exit(self, issue: Issue, attempt: RunAttempt):
        """Handle worker completion.

        _reconcile() owns cleanup for externally-cancelled workers (via
        _kill_worker + _cleanup_issue_state). This method owns cleanup for
        naturally-completed workers. The _force_cancelled guard prevents
        double-processing (token aggregation is not idempotent).
        """
        # If reconciliation already killed and cleaned up this worker, skip
        # all post-exit logic — no transitions, retries, or token aggregation.
        if issue.id in self._force_cancelled:
            self._force_cancelled.discard(issue.id)
            return

        self.total_input_tokens += attempt.input_tokens
        self.total_output_tokens += attempt.output_tokens
        self.total_tokens += attempt.total_tokens
        if attempt.started_at:
            elapsed = (datetime.now(timezone.utc) - attempt.started_at).total_seconds()
            self.total_seconds_running += elapsed

        issue_cfg = self._cfg_for_issue_or_primary(issue.id)
        if attempt.session_id:
            # Only persist session IDs for inherit-mode states.
            # Fresh sessions must not overwrite the stored ID, or the
            # next inherit-mode state resumes the wrong session.
            should_persist = True
            state_cfg = issue_cfg.states.get(attempt.state_name or "")
            if state_cfg and state_cfg.session == "fresh":
                should_persist = False
            if should_persist:
                self._last_session_ids[issue.id] = attempt.session_id

        completed_at = datetime.now(timezone.utc)
        attempt.completed_at = completed_at
        if attempt.status != "canceled":
            self._last_completed_at[issue.id] = completed_at

        self.running.pop(issue.id, None)
        self._tasks.pop(issue.id, None)

        if attempt.status == "succeeded":
            if attempt.state_name and attempt.state_name in issue_cfg.states:
                # State machine mode: use agent-requested transition or default
                transition_name = attempt.requested_transition or "complete"
                if not attempt.requested_transition:
                    logger.info(
                        f"No transition directive, defaulting to complete "
                        f"for {issue.identifier}"
                    )

                # Agent self-cancellation via reserved "cancel" transition
                if transition_name == "cancel":
                    logger.info(
                        f"Agent self-cancelled {issue.identifier}: "
                        f"{attempt.last_message[:200]}"
                    )
                    self._cleanup_issue_state(issue.id)
                    return

                # Safety cap: enforce max_rework for agent-initiated rework
                if transition_name != "complete":
                    sc = issue_cfg.states[attempt.state_name]
                    if sc.max_rework is not None:
                        run = self._issue_state_runs.get(issue.id, 1)
                        if run > sc.max_rework:
                            logger.warning(
                                f"Max rework ({sc.max_rework}) exceeded "
                                f"for {issue.identifier} in state "
                                f"{attempt.state_name}, forcing complete"
                            )
                            transition_name = "complete"

                asyncio.create_task(
                    self._safe_transition(issue, transition_name)
                )
            else:
                # Legacy mode
                self._schedule_retry(issue, attempt_num=1, delay_ms=1000)
        elif attempt.status == "config_error":
            # Unrecoverable config problem (hook template typo, Jinja2
            # syntax error, etc). A retry loop can't fix a config typo, so
            # mark the issue blocked and release the claim. On successful
            # hot-reload of workflow.yaml, _load_workflow clears
            # _config_blocked and the issue dispatches again.
            self._config_blocked.add(issue.id)
            self.claimed.discard(issue.id)
            self.running.pop(issue.id, None)
            self._tasks.pop(issue.id, None)
            logger.warning(
                f"Config error blocking issue={issue.identifier}: "
                f"{attempt.error}. Dispatch will resume after "
                f"workflow.yaml is reloaded with a valid config."
            )
        elif attempt.status in ("failed", "timed_out", "stalled"):
            current_attempt = (attempt.attempt or 0) + 1
            delay = min(
                10_000 * (2 ** (current_attempt - 1)),
                issue_cfg.agent.max_retry_backoff_ms,
            )
            self._schedule_retry(
                issue,
                attempt_num=current_attempt,
                delay_ms=delay,
                error=attempt.error,
            )
        else:
            self.claimed.discard(issue.id)

        # Best-effort log retention cleanup after worker completes
        self._fire_and_forget(self._cleanup_logs())

    def _schedule_retry(
        self,
        issue: Issue,
        attempt_num: int,
        delay_ms: int,
        error: str | None = None,
    ):
        """Schedule a retry for an issue."""
        # Cancel existing retry
        if issue.id in self._retry_timers:
            self._retry_timers[issue.id].cancel()

        entry = RetryEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            attempt=attempt_num,
            due_at_ms=time.monotonic() * 1000 + delay_ms,
            error=error,
        )
        self.retry_attempts[issue.id] = entry

        loop = asyncio.get_running_loop()
        handle = loop.call_later(
            delay_ms / 1000,
            lambda: loop.create_task(self._handle_retry(issue.id)),
        )
        self._retry_timers[issue.id] = handle

        logger.info(
            f"Retry scheduled issue={issue.identifier} "
            f"attempt={attempt_num} delay={delay_ms}ms "
            f"error={error or 'continuation'}"
        )

    async def _handle_retry(self, issue_id: str):
        """Handle a retry timer firing."""
        entry = self.retry_attempts.pop(issue_id, None)
        self._retry_timers.pop(issue_id, None)

        if entry is None:
            return

        # Fetch fresh candidates using the issue's OWN project client + states.
        slug = self._issue_project.get(issue_id)
        if slug is None or slug not in self.configs:
            # Lost binding (project evicted, or never bound) — nothing to retry.
            self.claimed.discard(issue_id)
            logger.info(f"Retry: issue {entry.identifier} lost project binding, releasing")
            return
        pcfg = self.configs[slug].config
        try:
            client = self._linear_client_for(slug)
            candidates = await client.fetch_candidate_issues(
                pcfg.tracker.project_slug,
                pcfg.active_linear_states(),
            )
        except Exception as e:
            logger.warning(f"Retry candidate fetch failed ({slug}): {e}")
            self.claimed.discard(issue_id)
            return

        issue = None
        for c in candidates:
            if c.id == issue_id:
                issue = c
                break

        if issue is None:
            # No longer active
            self.claimed.discard(issue_id)
            logger.info(f"Retry: issue {entry.identifier} no longer active, releasing")
            return

        # Check slots — shared global budget from the primary config.
        available = max(
            self._primary_cfg().agent.max_concurrent_agents - len(self.running), 0
        )
        if available <= 0:
            # Re-queue
            self._schedule_retry(
                issue,
                attempt_num=entry.attempt,
                delay_ms=10_000,
                error="no available orchestrator slots",
            )
            return

        self._dispatch(issue, attempt_num=entry.attempt)

    async def _reconcile(self):
        """Reconcile running and gated issues against current Linear state.

        Multi-project: fetch each issue's state through ITS project's client
        and compare against ITS project's state names (active/terminal/review
        differ per project).

        Unit 8: also reconciles templates for R21 hard-delete detection.
        Templates absent from the Linear response for
        ``TEMPLATE_HARD_DELETE_THRESHOLD_TICKS`` consecutive ticks trigger
        ``_cascade_template_delete`` (cancel in-flight children, remove
        persistent workspace, clear all per-template state). A single
        blip resets the counter — the fetch is best-effort and transient
        errors must not poison the threshold.
        """
        ids_to_check = (
            set(self.running) | set(self._pending_gates) | set(self._templates)
        )
        if not ids_to_check:
            return

        # Group IDs by project (fallback to primary for un-bound issues).
        primary_slug = next(iter(self.configs.keys())) if self.configs else None
        by_project: dict[str, list[str]] = {}
        for issue_id in ids_to_check:
            slug = self._issue_project.get(issue_id) or primary_slug
            if slug is None:
                continue
            by_project.setdefault(slug, []).append(issue_id)

        states: dict[str, str] = {}
        for slug, ids in by_project.items():
            try:
                client = self._linear_client_for(slug)
                proj_states = await client.fetch_issue_states_by_ids(ids)
            except Exception as e:
                logger.warning(f"Reconciliation state fetch failed ({slug}): {e}")
                continue
            states.update(proj_states)

        for issue_id in list(ids_to_check):
            current_state = states.get(issue_id)
            # Look up this issue's project config for state-name comparison.
            slug = self._issue_project.get(issue_id) or primary_slug
            issue_cfg = (
                self.configs[slug].config
                if slug and slug in self.configs
                else self._primary_cfg()
            )
            terminal_lower = [
                s.strip().lower() for s in issue_cfg.terminal_linear_states()
            ]
            active_lower = [
                s.strip().lower() for s in issue_cfg.active_linear_states()
            ]
            review_lower = issue_cfg.linear_states.review.strip().lower()

            if current_state is None:
                # Issue not found in Linear — may be deleted/archived.
                # Discriminate: template vs gated vs running.
                kind = classify_missing_id(
                    issue_id,
                    self._templates,
                    self.running,
                    self._pending_gates,
                )
                if kind == "template":
                    # Template absent: apply N-tick threshold to avoid
                    # mistaking a transient blip for a hard delete.
                    n = self._template_last_seen.get(issue_id, 0) + 1
                    self._template_last_seen[issue_id] = n
                    if n >= TEMPLATE_HARD_DELETE_THRESHOLD_TICKS:
                        await self._cascade_template_delete(issue_id)
                    continue
                if kind == "gated":
                    logger.info(
                        f"Reconciliation: gated issue {issue_id} not found in Linear, cleaning up"
                    )
                    cached = self._last_issues.get(issue_id)
                    if cached:
                        await self._remove_workspace_for_child(issue_id, cached.identifier)
                    self._cleanup_issue_state(issue_id)
                # "running" and "unknown" — skip (existing behavior)
                continue

            # Template is still present in Linear — reset the absent
            # counter so earlier transient errors don't accumulate.
            if issue_id in self._templates:
                self._template_last_seen.pop(issue_id, None)
                # Template is present + alive; no further child-oriented
                # state handling applies to templates here.
                continue

            state_lower = current_state.strip().lower()

            if state_lower in terminal_lower:
                logger.info(
                    f"Reconciliation: {issue_id} is terminal ({current_state}), stopping"
                )
                self._force_cancelled.add(issue_id)

                if issue_id in self.running:
                    await self._kill_worker(issue_id, reason=f"terminal state {current_state}")

                    attempt = self.running.get(issue_id)
                    if attempt:
                        await self._remove_workspace_for_child(
                            issue_id, attempt.issue_identifier
                        )
                else:
                    cached = self._last_issues.get(issue_id)
                    if cached:
                        await self._remove_workspace_for_child(
                            issue_id, cached.identifier
                        )

                self._cleanup_issue_state(issue_id)
                self._fire_and_forget(
                    self._post_cancellation_comment(issue_id, current_state)
                )

            elif state_lower == review_lower:
                # In review/gate state — kill worker subprocess but keep gate tracking
                if issue_id in self.running:
                    self._force_cancelled.add(issue_id)
                    await self._kill_worker(issue_id, reason=f"review state {current_state}")
                    self.running.pop(issue_id, None)
                    self._tasks.pop(issue_id, None)

            elif state_lower not in active_lower:
                # Neither active nor terminal nor review - kill and clean up
                if issue_id in self.running:
                    logger.info(
                        f"Reconciliation: {issue_id} not active ({current_state}), stopping"
                    )
                    self._force_cancelled.add(issue_id)
                    await self._kill_worker(issue_id, reason=f"non-active state {current_state}")
                    self._cleanup_issue_state(issue_id)

    def get_state_snapshot(self) -> dict[str, Any]:
        """Get current runtime state for observability."""
        now = datetime.now(timezone.utc)
        active_seconds = sum(
            (now - r.started_at).total_seconds()
            for r in self.running.values()
            if r.started_at
        )

        return {
            "generated_at": now.isoformat(),
            "counts": {
                "running": len(self.running),
                "retrying": len(self.retry_attempts),
                "gates": len(self._pending_gates),
            },
            "running": [
                {
                    "issue_id": r.issue_id,
                    "issue_identifier": r.issue_identifier,
                    "session_id": r.session_id,
                    "turn_count": r.turn_count,
                    "status": r.status,
                    "last_event": r.last_event,
                    "last_message": r.last_message,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "last_event_at": (
                        r.last_event_at.isoformat() if r.last_event_at else None
                    ),
                    "tokens": {
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                        "total_tokens": r.total_tokens,
                    },
                    "state_name": r.state_name,
                    "container_name": r.container_name,
                    "workflow": self._issue_workflow.get(r.issue_id),
                    "project_slug": self._issue_project.get(r.issue_id),
                }
                for r in self.running.values()
            ],
            "retrying": [
                {
                    "issue_id": e.issue_id,
                    "issue_identifier": e.identifier,
                    "attempt": e.attempt,
                    "error": e.error,
                    "project_slug": self._issue_project.get(e.issue_id),
                }
                for e in self.retry_attempts.values()
            ],
            "gates": [
                {
                    "issue_id": issue_id,
                    "issue_identifier": self._last_issues.get(issue_id, Issue(id="", identifier=issue_id, title="")).identifier,
                    "gate_state": gate_state,
                    "run": self._issue_state_runs.get(issue_id, 1),
                    "workflow": self._issue_workflow.get(issue_id),
                    "project_slug": self._issue_project.get(issue_id),
                }
                for issue_id, gate_state in self._pending_gates.items()
            ],
            "totals": {
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "total_tokens": self.total_tokens,
                "seconds_running": round(
                    self.total_seconds_running + active_seconds, 1
                ),
            },
        }


# ---------------------------------------------------------------------------
# Log retention helpers (module-level pure functions for testability)
# ---------------------------------------------------------------------------


def cleanup_old_logs(log_dir: Path, max_age_days: int) -> int:
    """Delete log files older than max_age_days. Returns count of deleted files."""
    cutoff = time.time() - (max_age_days * 86400)
    deleted = 0
    for issue_dir in log_dir.iterdir():
        if not issue_dir.is_dir():
            continue
        for log_file in issue_dir.iterdir():
            if not log_file.is_file():
                continue
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
                    deleted += 1
            except OSError:
                pass
        # Remove empty issue directory
        try:
            if issue_dir.is_dir() and not any(issue_dir.iterdir()):
                issue_dir.rmdir()
        except OSError:
            pass
    if deleted:
        logger.info(f"Log retention: deleted {deleted} old log files")
    return deleted


def enforce_size_limit(
    log_dir: Path,
    max_total_size_mb: int,
    exempt_identifiers: set[str] | None = None,
) -> int:
    """Delete oldest log files when total size exceeds limit. Returns count deleted.

    Files in directories matching exempt_identifiers are skipped (active agents).
    """
    exempt = exempt_identifiers or set()
    max_bytes = max_total_size_mb * 1024 * 1024

    # Collect all log files with their sizes and mtimes
    files: list[tuple[Path, float, int]] = []  # (path, mtime, size)
    total_size = 0
    for issue_dir in log_dir.iterdir():
        if not issue_dir.is_dir():
            continue
        for log_file in issue_dir.iterdir():
            if not log_file.is_file():
                continue
            try:
                stat = log_file.stat()
                files.append((log_file, stat.st_mtime, stat.st_size))
                total_size += stat.st_size
            except OSError:
                pass

    if total_size <= max_bytes:
        return 0

    # Sort oldest first
    files.sort(key=lambda x: x[1])

    deleted = 0
    for path, mtime, size in files:
        if total_size <= max_bytes:
            break
        # Skip files for actively running agents
        if path.parent.name in exempt:
            continue
        try:
            path.unlink()
            total_size -= size
            deleted += 1
        except OSError:
            pass

    # Clean up empty directories
    for issue_dir in log_dir.iterdir():
        if not issue_dir.is_dir():
            continue
        try:
            if not any(issue_dir.iterdir()):
                issue_dir.rmdir()
        except OSError:
            pass

    if deleted:
        logger.info(f"Log retention: deleted {deleted} files to enforce size limit")
    if total_size > max_bytes:
        logger.warning(
            f"Log retention: still over size limit after cleanup "
            f"({total_size // (1024*1024)}MB > {max_total_size_mb}MB) — "
            f"remaining files may belong to active agents"
        )
    return deleted
