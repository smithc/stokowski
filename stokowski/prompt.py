"""Three-layer prompt assembly for state machine workflows.

Assembles prompts from:
1. Global prompt — loaded from a .md file referenced in config
2. Stage prompt — loaded from the state's prompt .md file
3. Lifecycle injection — auto-generated from config + Linear data
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import BaseLoader, Environment, StrictUndefined, Undefined

from .config import HooksConfig, LinearStatesConfig, RepoConfig, ServiceConfig, StateConfig
from .models import Issue
from .tracking import get_comments_since, get_last_tracking_timestamp

log = logging.getLogger(__name__)


def load_prompt_file(path: str, workflow_dir: str | Path) -> str:
    """Load a .md prompt file relative to the workflow directory.

    Args:
        path: File path (absolute or relative to workflow_dir).
        workflow_dir: Directory containing the workflow file.

    Returns:
        The file contents as a string.

    Raises:
        FileNotFoundError: If the resolved path does not exist.
    """
    p = Path(path)
    if not p.is_absolute():
        p = Path(workflow_dir) / p
    p = p.resolve()
    if not p.exists():
        raise FileNotFoundError(f"Prompt file not found: {p}")
    return p.read_text()


def render_template(template_str: str, context: dict[str, Any]) -> str:
    """Render a Jinja2 template string with the given context.

    Uses a permissive undefined handler so missing variables render as
    empty strings rather than raising errors. Intended for prompt rendering,
    where agent prompts benefit from forgiving templates.

    For **hook rendering**, use ``render_hook_template`` instead — hooks
    execute as shell and silent-undefined would let typos through as
    ``git clone $EMPTY``.
    """
    env = Environment(loader=BaseLoader(), undefined=_SilentUndefined)
    template = env.from_string(template_str)
    return template.render(**context)


def render_hook_template(
    hook_script: str, repo: RepoConfig
) -> str:
    """Render a hook shell script with repo metadata using StrictUndefined.

    Unlike ``render_template`` (which silently drops undefined variables),
    this raises on any typo or missing variable — hooks execute as shell
    and a silent empty-string substitution would produce dangerous behavior
    like ``git clone $EMPTY``.

    Context exposed: a nested ``repo`` namespace identical to the prompt
    context — ``{{ repo.name }}``, ``{{ repo.clone_url }}``, ``{{ repo.label }}``.

    This helper is ONLY invoked when the config has an explicit ``repos:``
    section (``cfg.repos_synthesized == False``). Legacy configs bypass
    Jinja rendering entirely so hook bodies with literal ``{``/``}``
    (e.g. shell function syntax ``!f() { ...; }; f``) continue to work
    unchanged.
    """
    env = Environment(loader=BaseLoader(), undefined=StrictUndefined)
    template = env.from_string(hook_script)
    return template.render(repo={
        "name": repo.name,
        "clone_url": repo.clone_url or "",
        "label": repo.label or "",
    })


def render_hooks_for_dispatch(
    hooks: HooksConfig, repo: RepoConfig | None, synthesized: bool
) -> HooksConfig:
    """Return a HooksConfig with fields Jinja-rendered over repo metadata.

    When ``synthesized`` is True (legacy 1:1 config, no ``repos:`` section
    in YAML), hook scripts are returned verbatim with NO rendering. This
    preserves R19 backward compatibility for configs containing literal
    ``{``/``}`` characters in shell bodies.

    When ``synthesized`` is False, each non-empty hook field is rendered
    with ``render_hook_template``. Undefined variable references raise
    ``jinja2.UndefinedError`` which the orchestrator catches and surfaces
    as a Linear comment on the ticket.

    The original ``hooks`` object is not mutated; a new ``HooksConfig`` is
    returned.
    """
    if synthesized or repo is None:
        return hooks

    def _render(script: str | None) -> str | None:
        return render_hook_template(script, repo) if script else script

    return HooksConfig(
        after_create=_render(hooks.after_create),
        before_run=_render(hooks.before_run),
        after_run=_render(hooks.after_run),
        before_remove=_render(hooks.before_remove),
        on_stage_enter=_render(hooks.on_stage_enter),
        timeout_ms=hooks.timeout_ms,
    )


class _SilentUndefined(Undefined):
    """Jinja2 undefined that renders as empty string instead of raising."""

    def __str__(self) -> str:
        return ""

    def __iter__(self) -> Any:
        return iter([])

    def __bool__(self) -> bool:
        return False

    def _fail_with_undefined_error(self, *args: Any, **kwargs: Any) -> Any:
        return _SilentUndefined()

    def __getattr__(self, name: str) -> _SilentUndefined:
        if name.startswith("_"):
            raise AttributeError(name)
        return _SilentUndefined()

    def __getitem__(self, name: str) -> _SilentUndefined:
        return _SilentUndefined()


def build_template_context(
    issue: Issue,
    state_name: str,
    run: int = 1,
    attempt: int = 1,
    last_run_at: str | None = None,
    repo: RepoConfig | None = None,
) -> dict[str, Any]:
    """Build the Jinja2 template context dict from issue and run metadata.

    Args:
        issue: The Linear issue being worked on.
        state_name: Internal state machine state name.
        run: Current run number for this state.
        attempt: Retry attempt within this run.
        last_run_at: ISO timestamp of the last run, if any.
        repo: The resolved RepoConfig for this dispatch. When provided, a
            nested ``repo`` namespace is exposed to templates as
            ``{{ repo.name }}``, ``{{ repo.clone_url }}``, ``{{ repo.label }}``.
            For the synthetic ``_default`` repo, ``clone_url`` and ``label``
            render as empty strings; ``name`` renders as ``_default``.

    Returns:
        A flat dict suitable for Jinja2 rendering. If ``repo`` is provided,
        the dict also contains a nested ``repo`` entry.
    """
    ctx: dict[str, Any] = {
        "issue_id": issue.id,
        "issue_identifier": issue.identifier,
        "issue_title": issue.title,
        "issue_description": issue.description or "",
        "issue_url": issue.url or "",
        "issue_priority": issue.priority,
        "issue_state": issue.state,
        "issue_branch": issue.branch_name or "",
        "issue_labels": issue.labels,
        "state_name": state_name,
        "run": run,
        "attempt": attempt,
        "last_run_at": last_run_at or "",
    }
    if repo is not None:
        ctx["repo"] = {
            "name": repo.name,
            "clone_url": repo.clone_url or "",
            "label": repo.label or "",
        }
    return ctx


def build_lifecycle_section(
    issue: Issue,
    state_name: str,
    state_cfg: StateConfig,
    linear_states: LinearStatesConfig,
    run: int = 1,
    is_rework: bool = False,
    recent_comments: list[dict[str, Any]] | None = None,
    transitions: dict[str, str] | None = None,
    repo: RepoConfig | None = None,
) -> str:
    """Generate the auto-injected lifecycle section.

    This section is appended to every prompt to give the agent context about
    the current issue, state, and what actions to take when done.

    Args:
        issue: The Linear issue.
        state_name: Internal state machine state name.
        state_cfg: Configuration for the current state.
        linear_states: Linear state name mappings.
        run: Current run number.
        is_rework: Whether this is a rework run after gate rejection.
        recent_comments: Non-tracking comments since last run.
        transitions: Workflow-specific transitions for this state. When
            provided, used instead of ``state_cfg.transitions`` for the
            "Available Transitions" and "When Done" sections. ``None``
            falls back to ``state_cfg.transitions`` (backward compat).
        repo: The resolved RepoConfig for this dispatch. Added as a
            ``**Repository:**`` line when the repo is not the synthetic
            ``_default`` legacy fallback. Omitted entirely for ``_default``
            to avoid noise in single-repo legacy configs.

    Returns:
        A markdown string clearly demarcated as auto-generated.
    """
    lines: list[str] = []

    lines.append("---")
    lines.append("<!-- AUTO-GENERATED BY STOKOWSKI — DO NOT EDIT -->")
    lines.append("")
    lines.append("## Lifecycle Context")
    lines.append("")
    lines.append(f"- **Issue:** {issue.identifier} — {issue.title}")
    if issue.url:
        lines.append(f"- **URL:** {issue.url}")
    # Only expose repo context in multi-repo mode. For the synthetic _default
    # repo (legacy 1:1 config), the block is omitted — the agent has always
    # inferred the codebase from cwd in that mode.
    if repo is not None and repo.name != "_default":
        lines.append(f"- **Repository:** {repo.name}")
        if repo.clone_url:
            lines.append(f"- **Clone URL:** {repo.clone_url}")
    lines.append(f"- **State:** {state_name}")
    lines.append(f"- **Run:** {run}")
    lines.append("")

    # Scope restriction guardrail
    lines.append("### Scope Restriction")
    lines.append("")
    lines.append(
        f"You are scoped to issue {issue.identifier} ONLY. Do not modify, "
        f"comment on, or transition any other Linear issue. You may read "
        f"other issues for context (e.g., checking a blocker's status), "
        f"but do not take any write action on them."
    )
    lines.append("")

    # Rework information
    if is_rework:
        lines.append("### Rework")
        lines.append("")
        lines.append(
            "This is a **rework run**. A previous submission was reviewed "
            "and sent back for changes."
        )
        lines.append("")
        if recent_comments:
            lines.append("**Review comments:**")
            lines.append("")
            for comment in recent_comments:
                body = comment.get("body", "").strip()
                created = comment.get("createdAt", "")
                if body:
                    lines.append(f"> {body}")
                    if created:
                        lines.append(f"> — {created}")
                    lines.append("")
        lines.append(
            "Address the feedback above before resubmitting."
        )
        lines.append("")

    # Recent activity (non-rework)
    if not is_rework and recent_comments:
        lines.append("### Recent Activity")
        lines.append("")
        for comment in recent_comments:
            body = comment.get("body", "").strip()
            created = comment.get("createdAt", "")
            if body:
                lines.append(f"> {body}")
                if created:
                    lines.append(f"> — {created}")
                lines.append("")

    # Available transitions — prefer workflow-specific when provided
    effective_transitions = transitions if transitions is not None else state_cfg.transitions
    if effective_transitions:
        lines.append("### Transitions")
        lines.append("")
        for trigger, target in effective_transitions.items():
            lines.append(f"- `{trigger}` → **{target}**")
        lines.append("")

    # Instructions for completion
    lines.append("### When Done")
    lines.append("")
    if len(effective_transitions) > 1:
        lines.append(
            "When you have completed your work, include a transition "
            "directive in your final message to indicate the next step:"
        )
        lines.append("")
        lines.append("```")
        lines.append("<!-- transition:TRANSITION_NAME -->")
        lines.append("```")
        lines.append("")
        lines.append(
            "where TRANSITION_NAME is one of the transitions listed above. "
            "If no directive is included, `complete` is used by default."
        )
        lines.append("")
    else:
        lines.append(
            "When you have completed your work, the `complete` transition "
            "will fire automatically. No special action is needed."
        )
        lines.append("")
    lines.append("<!-- END STOKOWSKI LIFECYCLE -->")

    return "\n".join(lines)


def assemble_prompt(
    cfg: ServiceConfig,
    workflow_dir: str | Path,
    issue: Issue,
    state_name: str,
    state_cfg: StateConfig,
    run: int = 1,
    is_rework: bool = False,
    attempt: int = 1,
    last_run_at: str | None = None,
    comments: list[dict[str, Any]] | None = None,
    transitions: dict[str, str] | None = None,
    repo: RepoConfig | None = None,
) -> str:
    """Orchestrate three-layer prompt assembly.

    Combines:
    1. Global prompt (from config's prompts.global_prompt path)
    2. Stage prompt (from state_cfg.prompt path)
    3. Lifecycle injection (auto-generated)

    Each layer is rendered as a Jinja2 template with the issue context.

    Args:
        cfg: The full service config.
        workflow_dir: Directory containing the workflow file.
        issue: The Linear issue.
        state_name: Internal state machine state name.
        state_cfg: Configuration for the current state.
        run: Current run number.
        is_rework: Whether this is a rework run.
        attempt: Retry attempt within this run.
        last_run_at: ISO timestamp of the last run.
        comments: All comments on the issue (for filtering).
        transitions: Workflow-specific transitions for this state. Passed
            through to ``build_lifecycle_section()``. ``None`` falls back
            to ``state_cfg.transitions``.

    Returns:
        The fully assembled prompt string.
    """
    context = build_template_context(
        issue=issue,
        state_name=state_name,
        run=run,
        attempt=attempt,
        last_run_at=last_run_at,
        repo=repo,
    )

    parts: list[str] = []

    # Layer 1: Global prompt
    if cfg.prompts.global_prompt:
        try:
            raw = load_prompt_file(cfg.prompts.global_prompt, workflow_dir)
            rendered = render_template(raw, context)
            parts.append(rendered)
        except FileNotFoundError:
            log.warning(
                "Global prompt file not found: %s", cfg.prompts.global_prompt
            )

    # Layer 2: Stage prompt
    if state_cfg.prompt:
        try:
            raw = load_prompt_file(state_cfg.prompt, workflow_dir)
            rendered = render_template(raw, context)
            parts.append(rendered)
        except FileNotFoundError:
            log.warning(
                "Stage prompt file not found for state '%s': %s",
                state_name,
                state_cfg.prompt,
            )

    # Layer 3: Lifecycle injection
    # Filter comments to recent non-tracking ones
    recent: list[dict[str, Any]] = []
    if comments:
        last_ts = get_last_tracking_timestamp(comments)
        recent = get_comments_since(comments, last_ts)

    lifecycle = build_lifecycle_section(
        issue=issue,
        state_name=state_name,
        state_cfg=state_cfg,
        linear_states=cfg.linear_states,
        run=run,
        is_rework=is_rework,
        recent_comments=recent,
        transitions=transitions,
        repo=repo,
    )
    parts.append(lifecycle)

    return "\n\n".join(parts)
