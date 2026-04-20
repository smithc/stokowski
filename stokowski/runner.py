"""Agent runner - launches Claude Code in headless mode and streams results."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ClaudeConfig, DockerConfig, HooksConfig
from .docker_runner import build_docker_run_args, container_name_for, kill_container
from .models import Issue, RunAttempt

logger = logging.getLogger("stokowski.runner")

# Pattern for agent-requested transition directives in result text
TRANSITION_PATTERN = re.compile(r"<!--\s*transition:(\w[\w-]*)\s*-->")

# Callback type for events from the runner to the orchestrator
EventCallback = Callable[[str, str, dict[str, Any]], None]
# Callback for registering/unregistering child PIDs
PidCallback = Callable[[int, bool], None]  # (pid, is_register)


def _prepare_docker_args(
    docker_cfg: DockerConfig | None,
    args: list[str],
    workspace_path: Path,
    workspace_key: str,
    issue: Issue,
    attempt: RunAttempt,
    env: dict[str, str] | None,
    docker_image: str = "",
    needs_plugin_config: bool = False,
) -> tuple[list[str], str | None, str | None, dict[str, str] | None]:
    """Wrap CLI args in docker run if Docker is enabled.

    Returns (args, container_name, cwd, env) -- when Docker is enabled,
    cwd and env are None (handled by docker run args).

    ``needs_plugin_config`` is forwarded to ``build_docker_run_args``. Only
    ``run_agent_turn`` (Claude Code) should set it to True.
    """
    if not (docker_cfg and docker_cfg.enabled):
        return args, None, str(workspace_path), env

    container_name = container_name_for(
        issue.identifier, attempt.turn_count + 1, attempt.attempt
    )
    attempt.container_name = container_name
    image = docker_image or docker_cfg.default_image
    docker_args = build_docker_run_args(
        docker_cfg=docker_cfg,
        image=image,
        command=args,
        workspace_key=workspace_key,
        env=env or {},
        container_name=container_name,
        needs_plugin_config=needs_plugin_config,
    )
    return docker_args, container_name, None, None


def build_scope_restriction(
    issue_identifier: str,
    template_identifier: str | None = None,
) -> str:
    """Build the scope-restriction system prompt.

    For regular issues, returns the base guardrail prohibiting writes to any
    Linear issue other than the one being worked on.

    For scheduled-job children (when ``template_identifier`` is set), extends
    the guardrail with an explicit carve-out permitting comment writes on the
    parent template for cross-fire status coordination (R25). Writes to any
    OTHER Linear issue remain prohibited.

    This is a probabilistic guardrail enforced by prompting, not by tool
    permission (see CLAUDE.md "Agent scope guardrails").
    """
    base = (
        f"Do NOT use Linear tools to modify, comment on, or transition any "
        f"Linear issue other than {issue_identifier}. You may read other "
        f"issues for context, but do not take any write action on them."
    )
    if template_identifier is None:
        return base
    carve_out = (
        f"\n\nEXCEPTION for scheduled-job children: You MAY post comments on "
        f"your parent template {template_identifier} for cross-fire status "
        f"coordination (e.g., 'last run wrote 3 new entries to .context/'). "
        f"You MUST NOT write to any OTHER Linear issue — only your own issue "
        f"({issue_identifier}) and your parent template "
        f"({template_identifier})."
    )
    return base + carve_out


# Backwards-compatibility alias: legacy callers used a module-level constant
# with a ``{issue_identifier}`` slot. Prefer ``build_scope_restriction()``.
SCOPE_RESTRICTION_SYSTEM = (
    "Do NOT use Linear tools to modify, comment on, or transition any Linear issue "
    "other than {issue_identifier}. You may read other issues for context, but do "
    "not take any write action on them."
)


def build_claude_args(
    claude_cfg: ClaudeConfig,
    prompt: str,
    workspace_path: Path,
    session_id: str | None = None,
    issue_identifier: str | None = None,
    template_identifier: str | None = None,
) -> list[str]:
    """Build the claude CLI argument list.

    When ``template_identifier`` is provided, the scope-restriction guardrail
    is extended to permit comment writes to the parent template issue. This
    is used for scheduled-job children so they can post cross-fire status
    updates on their parent template.
    """
    args = [claude_cfg.command]

    if session_id:
        # Continuation turn
        args.extend(["-p", prompt, "--resume", session_id])
    else:
        # First turn
        args.extend(["-p", prompt])

    args.extend(["--verbose", "--output-format", "stream-json"])

    # Permission mode
    if claude_cfg.permission_mode == "auto":
        args.append("--dangerously-skip-permissions")
    elif claude_cfg.permission_mode == "allowedTools" and claude_cfg.allowed_tools:
        args.extend(["--allowedTools", ",".join(claude_cfg.allowed_tools)])

    # Model override
    if claude_cfg.model:
        args.extend(["--model", claude_cfg.model])

    # System prompt - always include headless context, plus any user additions
    if not session_id:
        headless_context = (
            "You are running in headless/unattended mode via Stokowski orchestrator. "
            "Do NOT use plan mode or wait for human input. "
            "You MAY use the Skill tool and Agent tool — when invoking skills, "
            "operate in pipeline mode and skip all interactive prompts."
        )
        # Scope restriction guardrail — prohibit writing to other Linear issues.
        # For scheduled-job children, extend with a carve-out permitting writes
        # to the parent template (passed via template_identifier).
        if issue_identifier:
            guardrail = build_scope_restriction(
                issue_identifier,
                template_identifier=template_identifier,
            )
            headless_context = f"{headless_context}\n{guardrail}"

        extra = claude_cfg.append_system_prompt or ""
        combined = f"{headless_context}\n{extra}".strip()
        args.extend(["--append-system-prompt", combined])

    return args


def build_codex_args(
    model: str | None,
    prompt: str,
    workspace_path: Path,
) -> list[str]:
    """Build the codex CLI argument list."""
    args = ["codex", "--quiet"]
    if model:
        args.extend(["--model", model])
    args.extend(["--prompt", prompt])
    return args


async def run_codex_turn(
    model: str | None,
    hooks_cfg: HooksConfig,
    prompt: str,
    workspace_path: Path,
    issue: Issue,
    attempt: RunAttempt,
    on_pid: PidCallback | None = None,
    turn_timeout_ms: int = 3_600_000,
    stall_timeout_ms: int = 300_000,
    env: dict[str, str] | None = None,
    docker_cfg: DockerConfig | None = None,
    workspace_key: str = "",
    docker_image: str = "",
    log_path: Path | None = None,
) -> RunAttempt:
    """Run a single Codex turn. Returns updated RunAttempt.

    Codex doesn't support session resumption or stream-json output.
    We capture stdout/stderr and use exit code for status.
    """
    args = build_codex_args(model, prompt, workspace_path)

    # Docker wrapping
    args, container_name, sub_cwd, sub_env = _prepare_docker_args(
        docker_cfg, args, workspace_path, workspace_key, issue, attempt, env, docker_image
    )

    logger.info(
        f"Launching codex issue={issue.identifier} "
        f"turn={attempt.turn_count + 1}"
    )

    # Run before_run hook
    if hooks_cfg.before_run:
        from .workspace import run_hook

        ok = await run_hook(
            hooks_cfg.before_run, workspace_path, hooks_cfg.timeout_ms, "before_run",
            docker_cfg=docker_cfg, docker_image=docker_image, workspace_key=workspace_key,
        )
        if not ok:
            attempt.status = "failed"
            attempt.error = "before_run hook failed"
            return attempt

    attempt.status = "streaming"
    attempt.started_at = attempt.started_at or datetime.now(timezone.utc)
    attempt.turn_count += 1
    attempt.last_event_at = datetime.now(timezone.utc)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=sub_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=10 * 1024 * 1024,  # 10MB line buffer (default 64KB)
            env=sub_env,
        )
        if on_pid and proc.pid:
            on_pid(proc.pid, True)
        attempt.pid = proc.pid
    except FileNotFoundError:
        attempt.status = "failed"
        attempt.error = "Docker command not found" if container_name else "Codex command not found: codex"
        logger.error(attempt.error)
        return attempt

    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    stall_timeout_s = stall_timeout_ms / 1000
    turn_timeout_s = turn_timeout_ms / 1000

    # Open log file for raw stdout capture (best-effort).
    # Opened after subprocess creation to avoid file handle leak on launch failure.
    log_file = None
    if log_path:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "wb")
        except OSError as e:
            logger.warning(f"Failed to open log file {log_path}: {e}")

    async def read_stream():
        nonlocal last_activity
        output_lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            # Write raw bytes to log file before any processing
            if log_file:
                try:
                    log_file.write(line)
                    log_file.flush()
                except OSError:
                    pass
            last_activity = loop.time()
            attempt.last_event_at = datetime.now(timezone.utc)
            line_str = line.decode().strip()
            if line_str:
                output_lines.append(line_str)
                attempt.last_message = line_str[:200]
        return output_lines

    async def stall_monitor():
        while proc.returncode is None:
            await asyncio.sleep(min(stall_timeout_s / 4, 30))
            elapsed = loop.time() - last_activity
            if stall_timeout_s > 0 and elapsed > stall_timeout_s:
                logger.warning(
                    f"Codex stall detected issue={issue.identifier} "
                    f"elapsed={elapsed:.0f}s"
                )
                proc.kill()
                if container_name:
                    asyncio.create_task(kill_container(container_name))
                attempt.status = "stalled"
                attempt.error = f"No output for {elapsed:.0f}s"
                return

    try:
        reader = asyncio.create_task(read_stream())
        monitor = asyncio.create_task(stall_monitor())

        done, pending = await asyncio.wait(
            {reader, monitor},
            timeout=turn_timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            logger.warning(f"Codex turn timeout issue={issue.identifier}")
            proc.kill()
            if container_name:
                asyncio.create_task(kill_container(container_name))
            attempt.status = "timed_out"
            attempt.error = f"Turn exceeded {turn_timeout_s}s"
        else:
            await asyncio.wait_for(proc.wait(), timeout=30)

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error(f"Codex runner error issue={issue.identifier}: {e}")
        proc.kill()
        attempt.status = "failed"
        attempt.error = str(e)
        # Still need to run after_run hook and unregister PID below
    finally:
        # Close log file on all exit paths
        if log_file:
            try:
                log_file.close()
            except OSError:
                pass

    # Determine final status from exit code if not already set
    if attempt.status == "streaming":
        stderr_output = ""
        if proc.stderr:
            try:
                stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=5)
                stderr_output = stderr_bytes.decode()[:500]
            except (asyncio.TimeoutError, Exception):
                pass
        if proc.returncode == 0:
            attempt.status = "succeeded"
        else:
            attempt.status = "failed"
            attempt.error = f"Codex exit code {proc.returncode}: {stderr_output}"

    # Run after_run hook
    if hooks_cfg.after_run:
        from .workspace import run_hook

        await run_hook(
            hooks_cfg.after_run, workspace_path, hooks_cfg.timeout_ms, "after_run",
            docker_cfg=docker_cfg, docker_image=docker_image, workspace_key=workspace_key,
        )

    # Unregister PID
    if on_pid and proc.pid:
        on_pid(proc.pid, False)

    logger.info(
        f"Codex turn complete issue={issue.identifier} "
        f"status={attempt.status}"
    )

    return attempt


async def run_agent_turn(
    claude_cfg: ClaudeConfig,
    hooks_cfg: HooksConfig,
    prompt: str,
    workspace_path: Path,
    issue: Issue,
    attempt: RunAttempt,
    on_event: EventCallback | None = None,
    on_pid: PidCallback | None = None,
    env: dict[str, str] | None = None,
    docker_cfg: DockerConfig | None = None,
    workspace_key: str = "",
    docker_image: str = "",
    log_path: Path | None = None,
    template_identifier: str | None = None,
) -> RunAttempt:
    """Run a single Claude Code turn. Returns updated RunAttempt.

    When ``template_identifier`` is provided, the scope-restriction guardrail
    is extended to permit the agent to post comments on the parent scheduled
    template for cross-fire status coordination.
    """
    args = build_claude_args(
        claude_cfg, prompt, workspace_path, attempt.session_id,
        issue_identifier=issue.identifier,
        template_identifier=template_identifier,
    )

    # Docker wrapping — Claude Code needs plugin config rewriting
    args, container_name, sub_cwd, sub_env = _prepare_docker_args(
        docker_cfg, args, workspace_path, workspace_key, issue, attempt, env, docker_image,
        needs_plugin_config=True,
    )

    logger.info(
        f"Launching claude issue={issue.identifier} "
        f"session={attempt.session_id or 'new'} "
        f"turn={attempt.turn_count + 1}"
    )

    # Run before_run hook
    if hooks_cfg.before_run:
        from .workspace import run_hook

        ok = await run_hook(
            hooks_cfg.before_run, workspace_path, hooks_cfg.timeout_ms, "before_run",
            docker_cfg=docker_cfg, docker_image=docker_image, workspace_key=workspace_key,
        )
        if not ok:
            attempt.status = "failed"
            attempt.error = "before_run hook failed"
            return attempt

    attempt.status = "streaming"
    attempt.started_at = attempt.started_at or datetime.now(timezone.utc)
    attempt.turn_count += 1
    attempt.last_event_at = datetime.now(timezone.utc)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=sub_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=10 * 1024 * 1024,  # 10MB line buffer (default 64KB)
            env=sub_env,
        )
        if on_pid and proc.pid:
            on_pid(proc.pid, True)
        attempt.pid = proc.pid
    except FileNotFoundError:
        attempt.status = "failed"
        attempt.error = "Docker command not found" if container_name else f"Claude command not found: {claude_cfg.command}"
        logger.error(attempt.error)
        return attempt

    # Stream stdout (NDJSON events)
    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    stall_timeout_s = claude_cfg.stall_timeout_ms / 1000
    turn_timeout_s = claude_cfg.turn_timeout_ms / 1000

    # Open log file for raw stdout capture (best-effort).
    # Opened after subprocess creation to avoid file handle leak on launch failure.
    log_file = None
    if log_path:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "wb")
        except OSError as e:
            logger.warning(f"Failed to open log file {log_path}: {e}")

    async def read_stream():
        nonlocal last_activity
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            # Write raw bytes to log file before any processing
            if log_file:
                try:
                    log_file.write(line)
                    log_file.flush()
                except OSError:
                    pass
            last_activity = loop.time()
            attempt.last_event_at = datetime.now(timezone.utc)

            line_str = line.decode().strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            _process_event(event, attempt, on_event, issue.identifier)

    async def stall_monitor():
        while proc.returncode is None:
            await asyncio.sleep(min(stall_timeout_s / 4, 30))
            elapsed = loop.time() - last_activity
            if stall_timeout_s > 0 and elapsed > stall_timeout_s:
                logger.warning(
                    f"Stall detected issue={issue.identifier} "
                    f"elapsed={elapsed:.0f}s"
                )
                proc.kill()
                if container_name:
                    asyncio.create_task(kill_container(container_name))
                attempt.status = "stalled"
                attempt.error = f"No output for {elapsed:.0f}s"
                return

    try:
        reader = asyncio.create_task(read_stream())
        monitor = asyncio.create_task(stall_monitor())

        # Overall turn timeout
        done, pending = await asyncio.wait(
            {reader, monitor},
            timeout=turn_timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            # Turn timeout
            logger.warning(f"Turn timeout issue={issue.identifier}")
            proc.kill()
            if container_name:
                asyncio.create_task(kill_container(container_name))
            attempt.status = "timed_out"
            attempt.error = f"Turn exceeded {turn_timeout_s}s"
        else:
            # Wait for process to finish
            await asyncio.wait_for(proc.wait(), timeout=30)

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error(f"Runner error issue={issue.identifier}: {e}")
        proc.kill()
        attempt.status = "failed"
        attempt.error = str(e)
        return attempt
    finally:
        # Close log file on all exit paths (normal, stall, timeout, CancelledError)
        if log_file:
            try:
                log_file.close()
            except OSError:
                pass

    # Determine final status from exit code if not already set by stall/timeout
    if attempt.status == "streaming":
        if proc.returncode == 0:
            attempt.status = "succeeded"
        else:
            stderr_output = ""
            if proc.stderr:
                try:
                    stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=5)
                    stderr_output = stderr_bytes.decode()[:500]
                except (asyncio.TimeoutError, Exception):
                    pass
            attempt.status = "failed"
            attempt.error = f"Exit code {proc.returncode}: {stderr_output}"

    # Run after_run hook
    if hooks_cfg.after_run:
        from .workspace import run_hook

        await run_hook(
            hooks_cfg.after_run, workspace_path, hooks_cfg.timeout_ms, "after_run",
            docker_cfg=docker_cfg, docker_image=docker_image, workspace_key=workspace_key,
        )

    # Unregister PID
    if on_pid and proc.pid:
        on_pid(proc.pid, False)

    logger.info(
        f"Turn complete issue={issue.identifier} "
        f"status={attempt.status} "
        f"tokens={attempt.total_tokens}"
    )

    return attempt


def _process_event(
    event: dict,
    attempt: RunAttempt,
    on_event: EventCallback | None,
    identifier: str,
):
    """Process a single NDJSON event from Claude Code stream-json output."""
    event_type = event.get("type", "")
    attempt.last_event = event_type

    # Extract session_id from result events
    if event_type == "result":
        if "session_id" in event:
            attempt.session_id = event["session_id"]
        # Extract token usage
        usage = event.get("usage", {})
        if usage:
            attempt.input_tokens = usage.get("input_tokens", attempt.input_tokens)
            attempt.output_tokens = usage.get("output_tokens", attempt.output_tokens)
            attempt.total_tokens = (
                usage.get("total_tokens", 0)
                or attempt.input_tokens + attempt.output_tokens
            )
        # Extract result text for last_message
        result_text = event.get("result", "")
        if isinstance(result_text, str) and result_text:
            attempt.last_message = result_text[:200]

            # Parse agent-requested transition directive (use LAST match
            # to avoid capturing quoted directives from earlier output)
            matches = TRANSITION_PATTERN.findall(result_text)
            if matches:
                attempt.requested_transition = matches[-1]
                logger.info(
                    f"Transition directive parsed issue={identifier} "
                    f"transition={matches[-1]}"
                )

    elif event_type == "assistant":
        # Assistant message content
        msg = event.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            attempt.last_message = content[:200]
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    attempt.last_message = block.get("text", "")[:200]
                    break

    elif event_type == "tool_use":
        tool_name = event.get("name", event.get("tool", ""))
        attempt.last_message = f"Using tool: {tool_name}"

    # Forward to orchestrator callback
    if on_event:
        on_event(identifier, event_type, event)


async def run_turn(
    runner_type: str,
    claude_cfg: ClaudeConfig,
    hooks_cfg: HooksConfig,
    prompt: str,
    workspace_path: Path,
    issue: Issue,
    attempt: RunAttempt,
    on_event: EventCallback | None = None,
    on_pid: PidCallback | None = None,
    env: dict[str, str] | None = None,
    docker_cfg: DockerConfig | None = None,
    workspace_key: str = "",
    docker_image: str = "",
    log_path: Path | None = None,
) -> RunAttempt:
    """Route to the correct runner based on runner_type."""
    if runner_type == "codex":
        return await run_codex_turn(
            model=claude_cfg.model,
            hooks_cfg=hooks_cfg,
            prompt=prompt,
            workspace_path=workspace_path,
            issue=issue,
            attempt=attempt,
            on_pid=on_pid,
            turn_timeout_ms=claude_cfg.turn_timeout_ms,
            stall_timeout_ms=claude_cfg.stall_timeout_ms,
            env=env,
            docker_cfg=docker_cfg,
            workspace_key=workspace_key,
            docker_image=docker_image,
            log_path=log_path,
        )
    elif runner_type == "claude":
        return await run_agent_turn(
            claude_cfg=claude_cfg,
            hooks_cfg=hooks_cfg,
            prompt=prompt,
            workspace_path=workspace_path,
            issue=issue,
            attempt=attempt,
            on_event=on_event,
            on_pid=on_pid,
            env=env,
            docker_cfg=docker_cfg,
            workspace_key=workspace_key,
            docker_image=docker_image,
            log_path=log_path,
        )
    else:
        raise ValueError(f"Unknown runner type: {runner_type}")
