"""Main orchestration loop - polls Linear, dispatches agents, manages state."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError

from .config import (
    ClaudeConfig,
    HooksConfig,
    PipelineConfig,
    ServiceConfig,
    StageConfig,
    WorkflowDefinition,
    load_stage_configs,
    merge_stage_config,
    parse_workflow_file,
    validate_config,
)
from .linear import LinearClient
from .models import Issue, RetryEntry, RunAttempt
from .runner import run_agent_turn, run_turn
from .tracking import (
    make_gate_comment,
    make_stage_comment,
    parse_latest_tracking,
    resolve_stage_index,
)
from .workspace import ensure_workspace, remove_workspace

logger = logging.getLogger("stokowski")


class Orchestrator:
    def __init__(self, workflow_path: str | Path):
        self.workflow_path = Path(workflow_path)
        self.workflow: WorkflowDefinition | None = None

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
        self._linear: LinearClient | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._retry_timers: dict[str, asyncio.TimerHandle] = {}
        self._child_pids: set[int] = set()  # Track claude subprocess PIDs
        self._last_session_ids: dict[str, str] = {}  # issue_id -> last known session_id
        self._jinja = Environment(undefined=StrictUndefined)
        self._running = False
        self._last_issues: dict[str, Issue] = {}
        self._last_completed_at: dict[str, datetime] = {}  # issue_id -> last worker completion time

        # Pipeline stage tracking
        self._stage_configs: dict[str, StageConfig] = {}
        self._issue_stages: dict[str, int] = {}
        self._issue_pipeline_runs: dict[str, int] = {}
        self._pending_gates: dict[str, str] = {}

    @property
    def cfg(self) -> ServiceConfig:
        assert self.workflow is not None
        return self.workflow.config

    def _load_workflow(self) -> list[str]:
        """Load/reload workflow file. Returns validation errors."""
        try:
            self.workflow = parse_workflow_file(self.workflow_path)
        except Exception as e:
            return [f"Workflow load error: {e}"]
        return validate_config(self.cfg)

    def _load_stage_configs(self) -> list[str]:
        """Load stage configs if pipeline is defined. Returns errors."""
        if not self.cfg.pipeline:
            self._stage_configs = {}
            return []
        try:
            self._stage_configs = load_stage_configs(
                self.workflow_path, self.cfg.pipeline
            )
            return []
        except Exception as e:
            return [f"Stage config load error: {e}"]

    def _ensure_linear_client(self) -> LinearClient:
        if self._linear is None:
            self._linear = LinearClient(
                endpoint=self.cfg.tracker.endpoint,
                api_key=self.cfg.resolved_api_key(),
            )
        return self._linear

    async def start(self):
        """Start the orchestration loop."""
        errors = self._load_workflow()
        if errors:
            for e in errors:
                logger.error(f"Config error: {e}")
            raise RuntimeError(f"Startup validation failed: {errors}")

        logger.info(
            f"Starting Stokowski "
            f"project={self.cfg.tracker.project_slug} "
            f"max_agents={self.cfg.agent.max_concurrent_agents} "
            f"poll_ms={self.cfg.polling.interval_ms}"
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

            # Interruptible sleep
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.cfg.polling.interval_ms / 1000,
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

        # Cancel async tasks
        for issue_id, task in list(self._tasks.items()):
            task.cancel()
        # Give them a moment to finish
        if self._tasks:
            await asyncio.sleep(0.5)
        self._tasks.clear()

        if self._linear:
            await self._linear.close()

    async def _startup_cleanup(self):
        """Remove workspaces for issues already in terminal states."""
        try:
            client = self._ensure_linear_client()
            terminal = await client.fetch_issues_by_states(
                self.cfg.tracker.project_slug,
                self.cfg.tracker.terminal_states,
            )
            ws_root = self.cfg.workspace.resolved_root()
            for issue in terminal:
                await remove_workspace(ws_root, issue.identifier, self.cfg.hooks)
            if terminal:
                logger.info(f"Cleaned {len(terminal)} terminal workspaces")
        except Exception as e:
            logger.warning(f"Startup cleanup failed (continuing): {e}")

    async def _resolve_current_stage(self, issue: Issue) -> tuple[int, str]:
        """Resolve current pipeline stage for an issue.
        Returns (stage_index, stage_name).
        """
        pipeline = self.cfg.pipeline
        assert pipeline is not None

        if issue.id in self._issue_stages:
            idx = self._issue_stages[issue.id]
            if idx < len(pipeline.stages):
                return idx, pipeline.stages[idx]

        client = self._ensure_linear_client()
        comments = await client.fetch_comments(issue.id)
        tracking = parse_latest_tracking(comments)

        if tracking is None:
            self._issue_stages[issue.id] = 0
            self._issue_pipeline_runs[issue.id] = 1
            return 0, pipeline.stages[0]

        if tracking["type"] == "stage":
            idx = tracking.get("index", 0)
            pipeline_run = tracking.get("pipeline_run", 1)
            if idx < len(pipeline.stages):
                self._issue_stages[issue.id] = idx
                self._issue_pipeline_runs[issue.id] = pipeline_run
                return idx, pipeline.stages[idx]
            self._issue_stages[issue.id] = 0
            self._issue_pipeline_runs[issue.id] = pipeline_run + 1
            return 0, pipeline.stages[0]

        if tracking["type"] == "gate":
            gate_name = tracking.get("gate", "")
            status = tracking.get("status", "")
            pipeline_run = tracking.get("pipeline_run", 1)

            if status == "waiting":
                idx = resolve_stage_index(pipeline.stages, gate_name)
                if idx is not None:
                    self._issue_stages[issue.id] = idx
                    self._pending_gates[issue.id] = gate_name
                    return idx, f"gate:{gate_name}"
            elif status == "approved":
                idx = resolve_stage_index(pipeline.stages, gate_name)
                if idx is not None and idx + 1 < len(pipeline.stages):
                    next_idx = idx + 1
                    self._issue_stages[issue.id] = next_idx
                    self._issue_pipeline_runs[issue.id] = pipeline_run
                    return next_idx, pipeline.stages[next_idx]
            elif status == "rework":
                rework_to = tracking.get("rework_to", "")
                for i, s in enumerate(pipeline.stages):
                    if s == rework_to:
                        self._issue_stages[issue.id] = i
                        self._issue_pipeline_runs[issue.id] = pipeline_run
                        return i, pipeline.stages[i]

        self._issue_stages[issue.id] = 0
        self._issue_pipeline_runs[issue.id] = 1
        return 0, pipeline.stages[0]

    async def _enter_gate(self, issue: Issue, gate_name: str):
        """Move issue to gate state and post tracking comment."""
        pipeline = self.cfg.pipeline
        assert pipeline is not None

        gate_cfg = pipeline.gates.get(gate_name)
        prompt = gate_cfg.prompt if gate_cfg else ""
        pipeline_run = self._issue_pipeline_runs.get(issue.id, 1)

        client = self._ensure_linear_client()

        comment = make_gate_comment(
            gate_name=gate_name,
            status="waiting",
            prompt=prompt,
            pipeline_run=pipeline_run,
        )
        await client.post_comment(issue.id, comment)

        gate_state = self.cfg.tracker.gate_states[0] if self.cfg.tracker.gate_states else "Awaiting Gate"
        await client.update_issue_state(issue.id, gate_state)

        self._pending_gates[issue.id] = gate_name
        # Release from running/claimed so it doesn't block slots
        self.running.pop(issue.id, None)
        self._tasks.pop(issue.id, None)
        self.claimed.discard(issue.id)

        logger.info(
            f"Gate entered issue={issue.identifier} gate={gate_name} "
            f"pipeline_run={pipeline_run}"
        )

    async def _advance_stage(self, issue: Issue):
        """Advance to the next pipeline stage after a successful stage completion."""
        pipeline = self.cfg.pipeline
        assert pipeline is not None

        current_idx = self._issue_stages.get(issue.id, 0)
        next_idx = current_idx + 1
        pipeline_run = self._issue_pipeline_runs.get(issue.id, 1)

        if next_idx >= len(pipeline.stages):
            logger.info(f"Pipeline complete issue={issue.identifier}")
            self._issue_stages.pop(issue.id, None)
            self._issue_pipeline_runs.pop(issue.id, None)
            self._pending_gates.pop(issue.id, None)
            self.claimed.discard(issue.id)
            return

        next_stage = pipeline.stages[next_idx]
        self._issue_stages[issue.id] = next_idx

        if next_stage.startswith("gate:"):
            gate_name = next_stage[5:]
            await self._enter_gate(issue, gate_name)
        else:
            client = self._ensure_linear_client()
            comment = make_stage_comment(
                stage=next_stage,
                index=next_idx,
                total=len(pipeline.stages),
                pipeline_run=pipeline_run,
            )
            await client.post_comment(issue.id, comment)
            self._schedule_retry(issue, attempt_num=0, delay_ms=1000)

    async def _handle_gate_responses(self):
        """Check for gate-approved and rework issues, handle transitions."""
        pipeline = self.cfg.pipeline
        if not pipeline:
            return

        # Skip if no gates in pipeline
        has_gates = any(s.startswith("gate:") for s in pipeline.stages)
        if not has_gates:
            return

        client = self._ensure_linear_client()

        # Fetch gate-approved issues
        try:
            approved_issues = await client.fetch_issues_by_states(
                self.cfg.tracker.project_slug,
                [self.cfg.tracker.gate_approved_state],
            )
        except Exception as e:
            logger.warning(f"Failed to fetch gate-approved issues: {e}")
            approved_issues = []

        for issue in approved_issues:
            if issue.id in self.running or issue.id in self.claimed:
                continue

            gate_name = self._pending_gates.pop(issue.id, None)
            if not gate_name:
                comments = await client.fetch_comments(issue.id)
                tracking = parse_latest_tracking(comments)
                if tracking and tracking.get("type") == "gate" and tracking.get("status") == "waiting":
                    gate_name = tracking.get("gate", "")

            if gate_name:
                pipeline_run = self._issue_pipeline_runs.get(issue.id, 1)
                comment = make_gate_comment(
                    gate_name=gate_name, status="approved", pipeline_run=pipeline_run,
                )
                await client.post_comment(issue.id, comment)

                gate_idx = resolve_stage_index(pipeline.stages, gate_name)
                if gate_idx is not None and gate_idx + 1 < len(pipeline.stages):
                    self._issue_stages[issue.id] = gate_idx + 1

                active_state = self.cfg.tracker.active_states[0] if self.cfg.tracker.active_states else "In Progress"
                await client.update_issue_state(issue.id, active_state)
                issue.state = active_state
                self._last_issues[issue.id] = issue
                logger.info(f"Gate approved issue={issue.identifier} gate={gate_name}")

        # Fetch rework issues
        try:
            rework_issues = await client.fetch_issues_by_states(
                self.cfg.tracker.project_slug,
                [self.cfg.tracker.rework_state],
            )
        except Exception as e:
            logger.warning(f"Failed to fetch rework issues: {e}")
            rework_issues = []

        for issue in rework_issues:
            if issue.id in self.running or issue.id in self.claimed:
                continue

            gate_name = self._pending_gates.pop(issue.id, None)
            if not gate_name:
                comments = await client.fetch_comments(issue.id)
                tracking = parse_latest_tracking(comments)
                if tracking and tracking.get("type") == "gate" and tracking.get("status") == "waiting":
                    gate_name = tracking.get("gate", "")

            if gate_name:
                gate_cfg = pipeline.gates.get(gate_name)
                rework_to = gate_cfg.rework_to if gate_cfg else ""
                if not rework_to:
                    logger.warning(f"Gate {gate_name} has no rework_to target, skipping")
                    continue

                pipeline_run = self._issue_pipeline_runs.get(issue.id, 1) + 1
                self._issue_pipeline_runs[issue.id] = pipeline_run

                comment = make_gate_comment(
                    gate_name=gate_name, status="rework",
                    rework_to=rework_to, pipeline_run=pipeline_run,
                )
                await client.post_comment(issue.id, comment)

                for i, s in enumerate(pipeline.stages):
                    if s == rework_to:
                        self._issue_stages[issue.id] = i
                        break

                active_state = self.cfg.tracker.active_states[0] if self.cfg.tracker.active_states else "In Progress"
                await client.update_issue_state(issue.id, active_state)
                issue.state = active_state
                self._last_issues[issue.id] = issue
                logger.info(f"Rework issue={issue.identifier} gate={gate_name} rework_to={rework_to} pipeline_run={pipeline_run}")

    async def _tick(self):
        """Single poll tick: reconcile, validate, fetch, dispatch."""
        # Reload workflow (supports hot-reload)
        errors = self._load_workflow()

        # Reload stage configs if pipeline is defined
        if self.cfg.pipeline:
            errors.extend(self._load_stage_configs())

        # Part 1: Reconcile running issues
        await self._reconcile()

        # Handle gate responses (pipeline mode)
        if self.cfg.pipeline:
            await self._handle_gate_responses()

        # Part 2: Validate config
        if errors:
            logger.warning(f"Config invalid, skipping dispatch: {errors}")
            return

        # Part 3: Fetch candidates
        try:
            client = self._ensure_linear_client()
            candidates = await client.fetch_candidate_issues(
                self.cfg.tracker.project_slug,
                self.cfg.tracker.active_states,
            )
        except Exception as e:
            logger.error(f"Failed to fetch candidates: {e}")
            return

        # Cache issues for retry lookup
        for issue in candidates:
            self._last_issues[issue.id] = issue

        # Part 4: Sort by priority
        candidates.sort(
            key=lambda i: (
                i.priority if i.priority is not None else 999,
                i.created_at or datetime.min.replace(tzinfo=timezone.utc),
                i.identifier,
            )
        )

        # Part 5: Dispatch
        available_slots = max(
            self.cfg.agent.max_concurrent_agents - len(self.running), 0
        )

        for issue in candidates:
            if available_slots <= 0:
                break
            if not self._is_eligible(issue):
                continue

            # Per-state concurrency check
            state_key = issue.state.strip().lower()
            state_limit = self.cfg.agent.max_concurrent_agents_by_state.get(state_key)
            if state_limit is not None:
                state_count = sum(
                    1
                    for r in self.running.values()
                    if self._last_issues.get(r.issue_id, Issue(id="", identifier="", title="")).state.strip().lower()
                    == state_key
                )
                if state_count >= state_limit:
                    continue

            self._dispatch(issue)
            available_slots -= 1

    def _is_eligible(self, issue: Issue) -> bool:
        """Check if an issue is eligible for dispatch."""
        if not issue.id or not issue.identifier or not issue.title or not issue.state:
            return False

        state_lower = issue.state.strip().lower()
        active_lower = [s.strip().lower() for s in self.cfg.tracker.active_states]
        terminal_lower = [s.strip().lower() for s in self.cfg.tracker.terminal_states]

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

        stage_name = None
        runner_type = "claude"
        if self.cfg.pipeline:
            if issue.id in self._issue_stages:
                idx = self._issue_stages[issue.id]
                if idx < len(self.cfg.pipeline.stages):
                    stage_name = self.cfg.pipeline.stages[idx]
            if stage_name and stage_name.startswith("gate:"):
                asyncio.create_task(self._enter_gate(issue, stage_name[5:]))
                return
            stage_cfg = self._stage_configs.get(stage_name) if stage_name else None
            if stage_cfg:
                runner_type = stage_cfg.runner

        attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=attempt_num,
            stage=stage_name,
            runner_type=runner_type,
        )

        # Session handling
        use_fresh_session = False
        if self.cfg.pipeline and stage_name:
            stage_cfg = self._stage_configs.get(stage_name)
            if stage_cfg and stage_cfg.session == "fresh":
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

        logger.info(
            f"Dispatched issue={issue.identifier} "
            f"state={issue.state} "
            f"stage={stage_name or 'legacy'} "
            f"runner={runner_type} "
            f"session={'fresh' if use_fresh_session else 'inherit'} "
            f"attempt={attempt_num}"
        )

    async def _run_worker(self, issue: Issue, attempt: RunAttempt):
        """Worker coroutine: prepare workspace, run agent turns."""
        try:
            # Resolve pipeline stage (reads tracking comments on first dispatch)
            if self.cfg.pipeline and not attempt.stage:
                idx, stage_name = await self._resolve_current_stage(issue)
                attempt.stage = stage_name if not stage_name.startswith("gate:") else None
                if stage_name.startswith("gate:"):
                    # Issue should be at a gate, not running
                    await self._enter_gate(issue, stage_name[5:])
                    return
                stage_cfg = self._stage_configs.get(stage_name)
                if stage_cfg:
                    attempt.runner_type = stage_cfg.runner

            claude_cfg = self.cfg.claude
            hooks_cfg = self.cfg.hooks
            runner_type = attempt.runner_type

            if self.cfg.pipeline and attempt.stage:
                stage_cfg = self._stage_configs.get(attempt.stage)
                if stage_cfg:
                    claude_cfg, hooks_cfg = merge_stage_config(
                        stage_cfg, self.cfg.claude, self.cfg.hooks
                    )
                    runner_type = stage_cfg.runner

            ws_root = self.cfg.workspace.resolved_root()
            ws = await ensure_workspace(ws_root, issue.identifier, self.cfg.hooks)
            attempt.workspace_path = str(ws.path)

            # Post stage tracking comment (only for first stage — _advance_stage handles subsequent)
            if self.cfg.pipeline and attempt.stage:
                pipeline = self.cfg.pipeline
                idx = self._issue_stages.get(issue.id, 0)
                pipeline_run = self._issue_pipeline_runs.get(issue.id, 1)
                if idx == 0 and pipeline_run == 1 and (attempt.attempt is None or attempt.attempt == 0):
                    client = self._ensure_linear_client()
                    comment = make_stage_comment(
                        stage=attempt.stage,
                        index=idx,
                        total=len(pipeline.stages),
                        pipeline_run=pipeline_run,
                    )
                    await client.post_comment(issue.id, comment)

            prompt = self._render_prompt(issue, attempt.attempt, attempt.stage)

            max_turns = claude_cfg.max_turns
            for turn in range(max_turns):
                if turn > 0:
                    current_state = issue.state
                    try:
                        client = self._ensure_linear_client()
                        states = await client.fetch_issue_states_by_ids([issue.id])
                        current_state = states.get(issue.id, issue.state)
                        state_lower = current_state.strip().lower()
                        active_lower = [
                            s.strip().lower() for s in self.cfg.tracker.active_states
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

    def _render_prompt(
        self, issue: Issue, attempt_num: int | None, stage: str | None = None
    ) -> str:
        """Render the prompt template with issue context."""
        assert self.workflow is not None

        template_str = self.workflow.prompt_template
        if stage and stage in self._stage_configs:
            stage_template = self._stage_configs[stage].prompt_template
            if stage_template:
                template_str = stage_template

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
                stage=stage,
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
        """Handle worker completion."""
        self.total_input_tokens += attempt.input_tokens
        self.total_output_tokens += attempt.output_tokens
        self.total_tokens += attempt.total_tokens
        if attempt.started_at:
            elapsed = (datetime.now(timezone.utc) - attempt.started_at).total_seconds()
            self.total_seconds_running += elapsed

        if attempt.session_id:
            self._last_session_ids[issue.id] = attempt.session_id

        completed_at = datetime.now(timezone.utc)
        attempt.completed_at = completed_at
        if attempt.status != "canceled":
            self._last_completed_at[issue.id] = completed_at

        self.running.pop(issue.id, None)
        self._tasks.pop(issue.id, None)

        if attempt.status == "succeeded":
            if self.cfg.pipeline and attempt.stage:
                asyncio.create_task(self._advance_stage(issue))
            else:
                self._schedule_retry(issue, attempt_num=1, delay_ms=1000)
        elif attempt.status in ("failed", "timed_out", "stalled"):
            current_attempt = (attempt.attempt or 0) + 1
            delay = min(
                10_000 * (2 ** (current_attempt - 1)),
                self.cfg.agent.max_retry_backoff_ms,
            )
            self._schedule_retry(
                issue,
                attempt_num=current_attempt,
                delay_ms=delay,
                error=attempt.error,
            )
        else:
            self.claimed.discard(issue.id)

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

        # Fetch fresh candidates to check eligibility
        try:
            client = self._ensure_linear_client()
            candidates = await client.fetch_candidate_issues(
                self.cfg.tracker.project_slug,
                self.cfg.tracker.active_states,
            )
        except Exception as e:
            logger.warning(f"Retry candidate fetch failed: {e}")
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

        # Check slots
        available = max(
            self.cfg.agent.max_concurrent_agents - len(self.running), 0
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
        """Reconcile running issues against current Linear state."""
        if not self.running:
            return

        running_ids = list(self.running.keys())

        try:
            client = self._ensure_linear_client()
            states = await client.fetch_issue_states_by_ids(running_ids)
        except Exception as e:
            logger.warning(f"Reconciliation state fetch failed: {e}")
            return

        terminal_lower = [
            s.strip().lower() for s in self.cfg.tracker.terminal_states
        ]
        active_lower = [
            s.strip().lower() for s in self.cfg.tracker.active_states
        ]

        for issue_id in running_ids:
            current_state = states.get(issue_id)
            if current_state is None:
                continue

            state_lower = current_state.strip().lower()

            if state_lower in terminal_lower:
                # Terminal - stop worker and clean workspace
                logger.info(
                    f"Reconciliation: {issue_id} is terminal ({current_state}), stopping"
                )
                task = self._tasks.get(issue_id)
                if task:
                    task.cancel()

                attempt = self.running.get(issue_id)
                if attempt:
                    ws_root = self.cfg.workspace.resolved_root()
                    await remove_workspace(
                        ws_root, attempt.issue_identifier, self.cfg.hooks
                    )

                self.running.pop(issue_id, None)
                self._tasks.pop(issue_id, None)
                self.claimed.discard(issue_id)

            # Check for gate states (pipeline mode)
            elif self.cfg.pipeline:
                gate_lower = [
                    s.strip().lower() for s in self.cfg.tracker.gate_states
                ]
                if state_lower in gate_lower:
                    task = self._tasks.get(issue_id)
                    if task:
                        task.cancel()
                    self.running.pop(issue_id, None)
                    self._tasks.pop(issue_id, None)
                    continue
                elif state_lower not in active_lower:
                    # Neither active nor terminal nor gate - stop without cleanup
                    logger.info(
                        f"Reconciliation: {issue_id} not active ({current_state}), stopping"
                    )
                    task = self._tasks.get(issue_id)
                    if task:
                        task.cancel()
                    self.running.pop(issue_id, None)
                    self._tasks.pop(issue_id, None)
                    self.claimed.discard(issue_id)

            elif state_lower not in active_lower:
                # Neither active nor terminal - stop without cleanup
                logger.info(
                    f"Reconciliation: {issue_id} not active ({current_state}), stopping"
                )
                task = self._tasks.get(issue_id)
                if task:
                    task.cancel()
                self.running.pop(issue_id, None)
                self._tasks.pop(issue_id, None)
                self.claimed.discard(issue_id)

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
                    "stage": r.stage,
                    "runner_type": r.runner_type,
                }
                for r in self.running.values()
            ],
            "retrying": [
                {
                    "issue_id": e.issue_id,
                    "issue_identifier": e.identifier,
                    "attempt": e.attempt,
                    "error": e.error,
                }
                for e in self.retry_attempts.values()
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
