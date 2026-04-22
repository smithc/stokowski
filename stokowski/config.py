"""Workflow loader and typed configuration."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stokowski.models import Issue

import yaml

log = logging.getLogger(__name__)


@dataclass
class TrackerConfig:
    kind: str = "linear"
    endpoint: str = "https://api.linear.app/graphql"
    api_key: str = ""
    project_slug: str = ""


@dataclass
class PollingConfig:
    interval_ms: int = 30_000


@dataclass
class WorkspaceConfig:
    root: str = ""

    def resolved_root(self) -> Path:
        if self.root:
            return Path(os.path.expandvars(os.path.expanduser(self.root)))
        return Path(tempfile.gettempdir()) / "stokowski_workspaces"


@dataclass
class HooksConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    on_stage_enter: str | None = None
    timeout_ms: int = 60_000


@dataclass
class ClaudeConfig:
    command: str = "claude"
    permission_mode: str = "auto"  # "auto" or "allowedTools"
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]
    )
    model: str | None = None
    max_turns: int = 20
    turn_timeout_ms: int = 3_600_000
    stall_timeout_ms: int = 300_000
    append_system_prompt: str | None = None


@dataclass
class AgentConfig:
    max_concurrent_agents: int = 5
    max_retry_backoff_ms: int = 300_000
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)


@dataclass
class DockerConfig:
    """Docker isolation settings for agent containers."""
    enabled: bool = False
    default_image: str = ""
    inherit_claude_config: bool = True
    host_claude_dir: str = "~/.claude"
    host_claude_dir_mount: str = ""
    plugin_shim_host_path: str = ""
    plugin_shim_container_path: str = ""
    extra_env: list[str] = field(default_factory=list)
    extra_volumes: list[str] = field(default_factory=list)
    volume_prefix: str = "stokowski-ws"
    sessions_volume: str = "stokowski-sessions"
    init: bool = True


@dataclass
class ServerConfig:
    port: int | None = None


@dataclass
class LoggingConfig:
    """Agent run log retention configuration."""
    enabled: bool = False
    log_dir: str = ""
    max_age_days: int = 14
    max_total_size_mb: int = 500

    def resolved_log_dir(self, base: Path | None = None) -> Path:
        """Resolve ~ and $VAR in log_dir.

        Args:
            base: Base directory for resolving relative paths (e.g. workflow dir).
        """
        expanded = os.path.expanduser(os.path.expandvars(self.log_dir))
        p = Path(expanded)
        if not p.is_absolute() and base:
            p = base / p
        return p


@dataclass
class SessionPersistenceConfig:
    """Claude Code session-id persistence across orchestrator restarts.

    When enabled, the orchestrator writes `_last_session_ids` to a JSON file on
    every save-point mutation and reloads it on startup. This allows inherit-mode
    states to resume their prior Claude session after a restart instead of
    starting fresh. Fresh-mode states (`session: fresh`) never touch this file.
    """
    enabled: bool = True
    path: str = ""

    def resolved_path(
        self,
        workspace_root: Path,
        workflow_dir: Path | None = None,
    ) -> Path:
        """Resolve ~ and $VAR in path, with a sensible default.

        Args:
            workspace_root: Primary project's resolved workspace root. Used as
                the default location when ``path`` is empty.
            workflow_dir: Base directory for resolving relative paths (e.g.
                the directory of the primary workflow file).
        """
        if not self.path:
            return workspace_root / ".stokowski-sessions.json"
        expanded = os.path.expanduser(os.path.expandvars(self.path))
        p = Path(expanded)
        if not p.is_absolute() and workflow_dir:
            p = workflow_dir / p
        return p


@dataclass
class LinearStatesConfig:
    """Maps logical state names to actual Linear state names."""
    todo: str = "Todo"
    active: str = "In Progress"
    review: str = "Human Review"
    gate_approved: str = "Gate Approved"
    rework: str = "Rework"
    terminal: list[str] = field(default_factory=lambda: ["Done", "Closed", "Cancelled"])


@dataclass
class PromptsConfig:
    """Prompt file references."""
    global_prompt: str | None = None


@dataclass
class StateConfig:
    """A single state in the state machine."""
    name: str = ""
    type: str = "agent"              # "agent", "gate", "terminal"
    prompt: str | None = None        # path to prompt .md file
    linear_state: str = "active"     # key into LinearStatesConfig
    runner: str = "claude"
    model: str | None = None
    max_turns: int | None = None
    turn_timeout_ms: int | None = None
    stall_timeout_ms: int | None = None
    session: str = "inherit"
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    rework_to: str | None = None     # gate only
    max_rework: int | None = None    # gate only; also used for agent-initiated rework cap
    skip_labels: list[str] = field(default_factory=list)  # labels that auto-approve this gate
    transitions: dict[str, str] = field(default_factory=dict)
    hooks: HooksConfig | None = None
    docker_image: str | None = None           # Override default_image for this state


@dataclass
class WorkflowConfig:
    """A single named workflow — an ordered path through shared stages."""
    name: str = ""
    label: str | None = None          # Linear label for workflow selection; None = triage/default
    default: bool = False
    path: list[str] = field(default_factory=list)
    terminal_state: str = "terminal"  # key into LinearStatesConfig
    transitions: dict[str, dict[str, str]] = field(default_factory=dict)
    entry_state: str = ""             # first agent state in path (derived)
    triage: bool = False              # True for the triage workflow; multi-repo configs
                                       # with no default repo require exactly one workflow
                                       # with triage=True (see validate_config)


@dataclass
class RepoConfig:
    """A single repository registered in the repos: registry.

    The v1 shape is minimum-for-routing. Deferred to MVP+:
    per-repo extra_env and per-repo hooks overrides (see the multi-repo brainstorm).
    """
    name: str = ""                    # registry key; exposed to templates as repo.name
    label: str | None = None          # Linear label for repo selection (e.g. "repo:api")
    clone_url: str = ""               # clone URL used by root/templated hooks
    default: bool = False             # at most one repo may be default
    docker_image: str | None = None   # repo-level image (level 2 in the 3-level hybrid)


def derive_workflow_transitions(
    path: list[str], states: dict[str, StateConfig]
) -> dict[str, dict[str, str]]:
    """Derive per-state transitions from an ordered workflow path.

    For each adjacent pair (current, next) in the path:
    - Agent states get ``{current: {"complete": next}}``
    - Gate states get ``{current: {"approve": next, "rework_to": ...}}``
      where ``rework_to`` is the gate's explicit ``StateConfig.rework_to``
      or the nearest prior agent state in the path.
    - Terminal states get ``{current: {}}`` (empty transitions).

    Returns the full transitions dict keyed by state name.
    """
    transitions: dict[str, dict[str, str]] = {}
    for i, current in enumerate(path):
        sc = states.get(current)
        if sc is None:
            continue  # unknown state — validation will catch this later

        if sc.type == "terminal":
            transitions[current] = {}
            continue

        has_next = i + 1 < len(path)
        next_state = path[i + 1] if has_next else None

        if sc.type == "agent":
            if next_state is not None:
                transitions[current] = {"complete": next_state}
            else:
                transitions[current] = {}

        elif sc.type == "gate":
            gate_transitions: dict[str, str] = {}
            if next_state is not None:
                gate_transitions["approve"] = next_state
            # Resolve rework_to: explicit on StateConfig wins, else scan backward
            if sc.rework_to:
                gate_transitions["rework_to"] = sc.rework_to
            else:
                # Scan backward for nearest prior agent state
                for j in range(i - 1, -1, -1):
                    prev_sc = states.get(path[j])
                    if prev_sc and prev_sc.type == "agent":
                        gate_transitions["rework_to"] = path[j]
                        break
            transitions[current] = gate_transitions

    return transitions


@dataclass
class ParsedConfig:
    config: ServiceConfig
    prompt_template: str


@dataclass
class ServiceConfig:
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    session_persistence: SessionPersistenceConfig = field(
        default_factory=SessionPersistenceConfig
    )
    linear_states: LinearStatesConfig = field(default_factory=LinearStatesConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    states: dict[str, StateConfig] = field(default_factory=dict)
    workflows: dict[str, WorkflowConfig] = field(default_factory=dict)
    repos: dict[str, RepoConfig] = field(default_factory=dict)
    # Set by the parser: True when the repos: section was absent from the
    # source YAML and a synthetic `_default` entry was generated for backward
    # compatibility. Operators should never set this field directly.
    repos_synthesized: bool = False

    def resolved_api_key(self) -> str:
        key = self.tracker.api_key
        if not key:
            return os.environ.get("LINEAR_API_KEY", "")
        if key.startswith("$"):
            return os.environ.get(key[1:], "")
        return key

    def agent_env(self) -> dict[str, str]:
        """Build env vars to pass to agent subprocesses.

        Includes the parent process env plus Linear config from workflow.yaml,
        so agents can connect to Linear using the same credentials as Stokowski.
        """
        env = dict(os.environ)
        api_key = self.resolved_api_key()
        if api_key:
            env["LINEAR_API_KEY"] = api_key
        if self.tracker.project_slug:
            env["LINEAR_PROJECT_SLUG"] = self.tracker.project_slug
        if self.tracker.endpoint:
            env["LINEAR_ENDPOINT"] = self.tracker.endpoint
        return env

    def docker_env(self) -> dict[str, str]:
        """Build minimal env vars for Docker agent containers.

        Unlike agent_env() which inherits the full parent environment,
        Docker mode only forwards explicitly declared variables to
        maintain container isolation.
        """
        env: dict[str, str] = {}
        api_key = self.resolved_api_key()
        if api_key:
            env["LINEAR_API_KEY"] = api_key
        if self.tracker.project_slug:
            env["LINEAR_PROJECT_SLUG"] = self.tracker.project_slug
        if self.tracker.endpoint:
            env["LINEAR_ENDPOINT"] = self.tracker.endpoint
        if not self.docker.inherit_claude_config:
            ak = os.environ.get("ANTHROPIC_API_KEY", "")
            if ak:
                env["ANTHROPIC_API_KEY"] = ak
        for var_name in self.docker.extra_env:
            val = os.environ.get(var_name, "")
            if val:
                env[var_name] = val
        return env

    @property
    def entry_state(self) -> str | None:
        """Return the first agent state.

        If workflows are defined, delegates to the default workflow's
        entry_state. Otherwise falls back to scanning the states dict.
        """
        if self.workflows:
            for wf in self.workflows.values():
                if wf.default:
                    return wf.entry_state or None
            # No default workflow — fall through to legacy scan
        for name, sc in self.states.items():
            if sc.type == "agent":
                return name
        return None

    def resolve_workflow(self, issue: Issue) -> WorkflowConfig:
        """Resolve which workflow applies to an issue based on its labels.

        Iterates workflows and checks if the workflow's label matches any
        of the issue's labels (case-insensitive). First match wins.
        Falls back to the workflow marked ``default=True``.
        Raises ValueError if no default workflow is configured.
        """
        issue_labels_lower = [l.lower() for l in issue.labels]
        for wf in self.workflows.values():
            if wf.label is not None and wf.label.lower() in issue_labels_lower:
                return wf
        # No label match — return default
        for wf in self.workflows.values():
            if wf.default:
                return wf
        raise ValueError("No default workflow configured")

    def resolve_repo(self, issue: Issue) -> RepoConfig:
        """Resolve which repo applies to an issue based on its labels.

        Mirrors ``resolve_workflow``: iterate repos, case-insensitive label
        match (first wins), then fall back to the repo marked ``default=True``.

        For legacy configs (``repos_synthesized=True``) there is exactly one
        repo (``_default``) marked ``default=True``, so any issue resolves
        to it.
        """
        issue_labels_lower = [l.lower() for l in issue.labels]
        for repo in self.repos.values():
            if repo.label is not None and repo.label.lower() in issue_labels_lower:
                return repo
        for repo in self.repos.values():
            if repo.default:
                return repo
        raise ValueError("No default repo configured")

    def get_workflow(self, name: str) -> WorkflowConfig | None:
        """Look up a workflow by name. Returns None if not found."""
        return self.workflows.get(name)

    def active_linear_states(self) -> list[str]:
        """Return Linear state names that should be polled for candidates.

        Includes the todo state (pickup) and all agent state mappings.
        """
        ls = self.linear_states
        seen: list[str] = []
        # Always include the todo state so new issues get picked up
        if ls.todo and ls.todo not in seen:
            seen.append(ls.todo)
        for sc in self.states.values():
            if sc.type == "agent":
                linear_name = _resolve_linear_state_name(sc.linear_state, ls)
                if linear_name and linear_name not in seen:
                    seen.append(linear_name)
        return seen

    def gate_linear_states(self) -> list[str]:
        """Return Linear state names for all gate states."""
        ls = self.linear_states
        seen: list[str] = []
        for sc in self.states.values():
            if sc.type == "gate":
                linear_name = _resolve_linear_state_name(sc.linear_state, ls)
                if linear_name and linear_name not in seen:
                    seen.append(linear_name)
        return seen

    def terminal_linear_states(self) -> list[str]:
        """Return the terminal Linear state names."""
        return list(self.linear_states.terminal)


def _resolve_linear_state_name(key: str, ls: LinearStatesConfig) -> str:
    """Resolve a logical state key to the actual Linear state name."""
    mapping: dict[str, str] = {
        "active": ls.active,
        "review": ls.review,
        "gate_approved": ls.gate_approved,
        "rework": ls.rework,
        "todo": ls.todo,
        "terminal": ls.terminal[0] if ls.terminal else "Done",
    }
    return mapping.get(key, key)


def _resolve_env(val: str) -> str:
    if isinstance(val, str) and val.startswith("$"):
        return os.environ.get(val[1:], "")
    return val


def _coerce_int(val: Any, default: int) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _coerce_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str):
        return [s.strip() for s in val.split(",") if s.strip()]
    return []


def _parse_hooks(raw: dict[str, Any] | None) -> HooksConfig | None:
    """Parse a hooks dict into HooksConfig, returning None if empty."""
    if not raw:
        return None
    return HooksConfig(
        after_create=raw.get("after_create"),
        before_run=raw.get("before_run"),
        after_run=raw.get("after_run"),
        before_remove=raw.get("before_remove"),
        on_stage_enter=raw.get("on_stage_enter"),
        timeout_ms=_coerce_int(raw.get("timeout_ms"), 60_000),
    )


def _parse_state_config(name: str, raw: dict[str, Any]) -> StateConfig:
    """Parse a single state entry from YAML into StateConfig."""
    allowed = raw.get("allowed_tools")
    hooks_raw = raw.get("hooks")

    return StateConfig(
        name=name,
        type=str(raw.get("type", "agent")),
        prompt=raw.get("prompt"),
        linear_state=str(raw.get("linear_state", "active")),
        runner=str(raw.get("runner", "claude")),
        model=raw.get("model"),
        max_turns=raw.get("max_turns"),
        turn_timeout_ms=raw.get("turn_timeout_ms"),
        stall_timeout_ms=raw.get("stall_timeout_ms"),
        session=str(raw.get("session", "inherit")),
        permission_mode=raw.get("permission_mode"),
        allowed_tools=_coerce_list(allowed) if allowed is not None else None,
        rework_to=raw.get("rework_to"),
        max_rework=raw.get("max_rework"),
        skip_labels=_coerce_list(raw.get("skip_labels")),
        transitions=raw.get("transitions") or {},
        hooks=_parse_hooks(hooks_raw) if hooks_raw else None,
        docker_image=raw.get("docker_image") or (raw.get("docker", {}) or {}).get("image"),
    )


def merge_state_config(
    state: StateConfig, root_claude: ClaudeConfig, root_hooks: HooksConfig
) -> tuple[ClaudeConfig, HooksConfig]:
    """Merge state overrides with root defaults. Returns (claude_cfg, hooks_cfg)."""
    claude = ClaudeConfig(
        command=root_claude.command,
        permission_mode=state.permission_mode or root_claude.permission_mode,
        allowed_tools=state.allowed_tools if state.allowed_tools is not None else root_claude.allowed_tools,
        model=state.model or root_claude.model,
        max_turns=state.max_turns if state.max_turns is not None else root_claude.max_turns,
        turn_timeout_ms=state.turn_timeout_ms if state.turn_timeout_ms is not None else root_claude.turn_timeout_ms,
        stall_timeout_ms=state.stall_timeout_ms if state.stall_timeout_ms is not None else root_claude.stall_timeout_ms,
        append_system_prompt=root_claude.append_system_prompt,
    )
    hooks = state.hooks if state.hooks is not None else root_hooks
    return claude, hooks


def parse_workflow_file(path: str | Path) -> ParsedConfig:
    """Parse a workflow file (.yaml/.yml or .md with front matter) into config."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Workflow file not found: {path}")

    content = path.read_text()
    config_raw: dict[str, Any] = {}
    prompt_body = ""

    # Detect format: pure YAML or markdown with front matter
    if path.suffix in (".yaml", ".yml"):
        config_raw = yaml.safe_load(content) or {}
    elif content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            config_raw = yaml.safe_load(parts[1]) or {}
            prompt_body = parts[2]
    else:
        # Try parsing as pure YAML
        config_raw = yaml.safe_load(content) or {}

    if not isinstance(config_raw, dict):
        raise ValueError("Workflow file must contain a YAML mapping")

    prompt_template = prompt_body.strip()

    # Parse tracker
    t = config_raw.get("tracker", {}) or {}
    tracker = TrackerConfig(
        kind=str(t.get("kind", "linear")),
        endpoint=str(t.get("endpoint", "https://api.linear.app/graphql")),
        api_key=str(t.get("api_key", "")),
        project_slug=str(t.get("project_slug", "")),
    )

    # Parse polling
    p = config_raw.get("polling", {}) or {}
    polling = PollingConfig(interval_ms=_coerce_int(p.get("interval_ms"), 30_000))

    # Parse workspace
    w = config_raw.get("workspace", {}) or {}
    workspace = WorkspaceConfig(root=str(w.get("root", "")))

    # Parse hooks
    h = config_raw.get("hooks", {}) or {}
    hooks = HooksConfig(
        after_create=h.get("after_create"),
        before_run=h.get("before_run"),
        after_run=h.get("after_run"),
        before_remove=h.get("before_remove"),
        on_stage_enter=h.get("on_stage_enter"),
        timeout_ms=_coerce_int(h.get("timeout_ms"), 60_000),
    )

    # Parse claude
    c = config_raw.get("claude", {}) or {}
    claude = ClaudeConfig(
        command=str(c.get("command", "claude")),
        permission_mode=str(c.get("permission_mode", "auto")),
        allowed_tools=_coerce_list(c.get("allowed_tools"))
        or ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
        model=c.get("model"),
        max_turns=_coerce_int(c.get("max_turns"), 20),
        turn_timeout_ms=_coerce_int(c.get("turn_timeout_ms"), 3_600_000),
        stall_timeout_ms=_coerce_int(c.get("stall_timeout_ms"), 300_000),
        append_system_prompt=c.get("append_system_prompt"),
    )

    # Parse agent
    a = config_raw.get("agent", {}) or {}
    agent = AgentConfig(
        max_concurrent_agents=_coerce_int(a.get("max_concurrent_agents"), 5),
        max_retry_backoff_ms=_coerce_int(a.get("max_retry_backoff_ms"), 300_000),
        max_concurrent_agents_by_state=a.get("max_concurrent_agents_by_state") or {},
    )

    # Parse server
    s = config_raw.get("server", {}) or {}
    server = ServerConfig(port=s.get("port"))

    # Parse logging
    lg = config_raw.get("logging", {}) or {}
    logging_cfg = LoggingConfig(
        enabled=bool(lg.get("enabled", False)),
        log_dir=str(lg.get("log_dir", "")),
        max_age_days=_coerce_int(lg.get("max_age_days"), 14),
        max_total_size_mb=_coerce_int(lg.get("max_total_size_mb"), 500),
    )

    # Parse session_persistence
    sp = config_raw.get("session_persistence", {}) or {}
    session_persistence = SessionPersistenceConfig(
        enabled=bool(sp.get("enabled", True)),
        path=str(sp.get("path", "")),
    )

    # Parse linear_states
    ls_raw = config_raw.get("linear_states", {}) or {}
    linear_states = LinearStatesConfig(
        todo=str(ls_raw.get("todo", "Todo")),
        active=str(ls_raw.get("active", "In Progress")),
        review=str(ls_raw.get("review", "Human Review")),
        gate_approved=str(ls_raw.get("gate_approved", "Gate Approved")),
        rework=str(ls_raw.get("rework", "Rework")),
        terminal=_coerce_list(ls_raw.get("terminal")) or ["Done", "Closed", "Cancelled"],
    )

    # Parse prompts
    pr_raw = config_raw.get("prompts", {}) or {}
    prompts = PromptsConfig(
        global_prompt=pr_raw.get("global_prompt"),
    )

    # Parse docker
    dk = config_raw.get("docker", {}) or {}
    docker = DockerConfig(
        enabled=bool(dk.get("enabled", False)),
        default_image=str(dk.get("default_image", "")),
        inherit_claude_config=bool(dk.get("inherit_claude_config", True)),
        host_claude_dir=str(dk.get("host_claude_dir", "~/.claude")),
        host_claude_dir_mount=str(dk.get("host_claude_dir_mount", "")),
        plugin_shim_host_path=str(dk.get("plugin_shim_host_path", "")),
        plugin_shim_container_path=str(dk.get("plugin_shim_container_path", "")),
        extra_env=_coerce_list(dk.get("extra_env")),
        extra_volumes=_coerce_list(dk.get("extra_volumes")),
        volume_prefix=str(dk.get("volume_prefix", "stokowski-ws")),
        sessions_volume=str(dk.get("sessions_volume", "stokowski-sessions")),
        init=bool(dk.get("init", True)),
    )

    # Parse states
    states_raw = config_raw.get("states", {}) or {}
    states: dict[str, StateConfig] = {}
    for state_name, state_data in states_raw.items():
        sd = state_data or {}
        states[state_name] = _parse_state_config(state_name, sd)

    # Parse workflows
    workflows_raw = config_raw.get("workflows", {}) or {}
    workflows: dict[str, WorkflowConfig] = {}

    if workflows_raw:
        # Multi-workflow mode: parse each workflow entry
        for wf_name, wf_data in workflows_raw.items():
            wd = wf_data or {}
            label = wd.get("label")
            default = bool(wd.get("default", False))
            triage = bool(wd.get("triage", False))
            path = _coerce_list(wd.get("path"))
            terminal_state = str(wd.get("terminal_state", "terminal"))
            transitions = derive_workflow_transitions(path, states)
            # Find entry_state: first agent state in path
            entry = ""
            for name in path:
                sc = states.get(name)
                if sc and sc.type == "agent":
                    entry = name
                    break
            workflows[wf_name] = WorkflowConfig(
                name=wf_name,
                label=label,
                default=default,
                path=path,
                terminal_state=terminal_state,
                transitions=transitions,
                entry_state=entry,
                triage=triage,
            )
    else:
        # Legacy/backward compat: synthesize a single _default workflow
        # using StateConfig.transitions verbatim (do NOT call derive_workflow_transitions)
        path = list(states.keys())
        transitions = {name: dict(sc.transitions) for name, sc in states.items()}
        entry = ""
        for name, sc in states.items():
            if sc.type == "agent":
                entry = name
                break
        workflows["_default"] = WorkflowConfig(
            name="_default",
            label=None,
            default=True,
            path=path,
            terminal_state="terminal",
            transitions=transitions,
            entry_state=entry,
        )

    # Parse repos registry. Synthesize a _default entry when the section
    # is absent OR explicitly empty — both cases are treated as "no multi-repo
    # configured" and fall back to the single-repo legacy path. Explicit
    # empty emits a warning since it's likely an operator mistake.
    repos_raw = config_raw.get("repos", None)
    repos: dict[str, RepoConfig] = {}
    repos_synthesized = False

    if isinstance(repos_raw, dict) and repos_raw:
        # Explicit registry with at least one entry
        for repo_name, repo_data in repos_raw.items():
            rd = repo_data or {}
            repos[repo_name] = RepoConfig(
                name=repo_name,
                label=rd.get("label"),
                clone_url=str(rd.get("clone_url", "")),
                default=bool(rd.get("default", False)),
                docker_image=rd.get("docker_image"),
            )
    else:
        # Absent OR explicit empty dict — synthesize _default for backward compat.
        # Mirrors the multi-workflow _default synthesis at the workflows
        # branch above. The synthetic entry carries sentinel values
        # (empty clone_url, None label) and is exempt from R21 validation.
        if repos_raw is not None:
            # Explicit empty dict — operator probably meant to populate this.
            log.warning(
                "repos: section is present but empty — treating as legacy "
                "single-repo config. Populate entries to enable multi-repo "
                "routing, or remove the section to silence this warning."
            )
        repos["_default"] = RepoConfig(
            name="_default",
            label=None,
            clone_url="",
            default=True,
            docker_image=None,
        )
        repos_synthesized = True

    cfg = ServiceConfig(
        tracker=tracker,
        polling=polling,
        workspace=workspace,
        hooks=hooks,
        claude=claude,
        agent=agent,
        server=server,
        logging=logging_cfg,
        session_persistence=session_persistence,
        linear_states=linear_states,
        prompts=prompts,
        docker=docker,
        states=states,
        workflows=workflows,
        repos=repos,
        repos_synthesized=repos_synthesized,
    )

    return ParsedConfig(config=cfg, prompt_template=prompt_template)


def validate_config(cfg: ServiceConfig) -> list[str]:
    """Validate state machine config for dispatch readiness. Returns list of errors."""
    errors: list[str] = []

    # Basic tracker checks
    if cfg.tracker.kind != "linear":
        errors.append(f"Unsupported tracker kind: {cfg.tracker.kind}")
    if not cfg.resolved_api_key():
        errors.append("Missing tracker API key (set LINEAR_API_KEY or tracker.api_key)")
    if not cfg.tracker.project_slug:
        errors.append("Missing tracker.project_slug")

    if not cfg.states:
        errors.append("No states defined")
        return errors

    # Detect legacy vs multi-workflow mode
    is_legacy = len(cfg.workflows) == 1 and "_default" in cfg.workflows

    # Valid linear_state keys
    valid_linear_keys = {"active", "review", "gate_approved", "rework", "terminal"}

    has_agent = False
    has_terminal = False
    all_state_names = set(cfg.states.keys())

    for name, sc in cfg.states.items():
        # Check type
        if sc.type not in ("agent", "gate", "terminal"):
            errors.append(f"State '{name}' has invalid type: {sc.type}")
            continue

        if sc.type == "agent":
            has_agent = True
            # Agent states should have a prompt
            if not sc.prompt:
                errors.append(f"Agent state '{name}' is missing 'prompt' field")

        elif sc.type == "gate":
            if is_legacy:
                # Gates must have rework_to
                if not sc.rework_to:
                    errors.append(f"Gate state '{name}' is missing 'rework_to' field")
                elif sc.rework_to not in all_state_names:
                    errors.append(
                        f"Gate state '{name}' rework_to target '{sc.rework_to}' "
                        f"is not a defined state"
                    )
                # Gates must have approve transition
                if "approve" not in sc.transitions:
                    errors.append(f"Gate state '{name}' is missing 'approve' transition")
            # (in multi-workflow mode, gate rework_to and approve are validated per-workflow below)

        elif sc.type == "terminal":
            has_terminal = True

        # Validate linear_state key
        if sc.linear_state not in valid_linear_keys:
            errors.append(
                f"State '{name}' has invalid linear_state: '{sc.linear_state}' "
                f"(valid: {', '.join(sorted(valid_linear_keys))})"
            )

        # Validate all transitions point to existing states
        for trigger, target in sc.transitions.items():
            if target not in all_state_names:
                errors.append(
                    f"State '{name}' transition '{trigger}' points to "
                    f"unknown state '{target}'"
                )

    if not has_agent:
        errors.append("No agent states defined (need at least one state with type 'agent')")
    if not has_terminal:
        errors.append("No terminal states defined (need at least one state with type 'terminal')")

    # In multi-workflow mode, error if any StateConfig has explicit transitions
    if not is_legacy:
        for name, sc in cfg.states.items():
            if sc.transitions:
                errors.append(
                    f"State '{name}' has explicit transitions in multi-workflow mode; "
                    f"transitions are derived from workflow paths"
                )

    # --- Workflow validation ---
    # Exactly one default workflow
    default_count = sum(1 for wf in cfg.workflows.values() if wf.default)
    if default_count == 0:
        errors.append("No default workflow defined (exactly one workflow must have default: true)")
    elif default_count > 1:
        errors.append(
            f"Multiple default workflows defined ({default_count}); "
            f"exactly one workflow must have default: true"
        )

    # No duplicate labels across workflows
    seen_labels: dict[str, str] = {}  # label -> workflow name
    for wf in cfg.workflows.values():
        if wf.label is not None:
            label_lower = wf.label.lower()
            if label_lower in seen_labels:
                errors.append(
                    f"Duplicate label '{wf.label}' on workflows "
                    f"'{seen_labels[label_lower]}' and '{wf.name}'"
                )
            else:
                seen_labels[label_lower] = wf.name

    # Per-workflow validation
    all_referenced_states: set[str] = set()
    for wf in cfg.workflows.values():
        # Every path entry must reference an existing state
        for state_name in wf.path:
            if state_name not in all_state_names:
                errors.append(
                    f"Workflow '{wf.name}' path references non-existent state '{state_name}'"
                )

        all_referenced_states.update(wf.path)

        # Each workflow path must contain at least one agent state
        has_path_agent = any(
            cfg.states.get(s) and cfg.states[s].type == "agent"
            for s in wf.path
        )
        if not has_path_agent:
            errors.append(
                f"Workflow '{wf.name}' path contains no agent states "
                f"(need at least one state with type 'agent')"
            )

        # Each workflow path must end with a terminal state
        if wf.path:
            last_state = wf.path[-1]
            last_sc = cfg.states.get(last_state)
            if last_sc and last_sc.type != "terminal":
                errors.append(
                    f"Workflow '{wf.name}' path must end with a terminal state "
                    f"('{last_state}' has type '{last_sc.type}')"
                )
        else:
            errors.append(f"Workflow '{wf.name}' has an empty path")

        # Validate terminal_state key
        valid_terminal_keys = {"terminal", "todo", "active", "review", "gate_approved", "rework"}
        if wf.terminal_state not in valid_terminal_keys:
            errors.append(
                f"Workflow '{wf.name}' has invalid terminal_state: '{wf.terminal_state}' "
                f"(must be a valid LinearStatesConfig key)"
            )
        if wf.terminal_state in ("active",):
            log.warning(
                "Workflow '%s' terminal_state resolves to an active state "
                "('%s') — this could cause dispatch loops",
                wf.name, wf.terminal_state,
            )

        # Gate validation within path context
        for i, state_name in enumerate(wf.path):
            sc = cfg.states.get(state_name)
            if not sc or sc.type != "gate":
                continue

            # Gate must have resolvable approve (next state in path)
            wf_transitions = wf.transitions.get(state_name, {})
            if "approve" not in wf_transitions:
                errors.append(
                    f"Gate '{state_name}' in workflow '{wf.name}' has no resolvable "
                    f"approve transition (no next state in path)"
                )

            # Gate must have resolvable rework_to
            if "rework_to" not in wf_transitions:
                # Check if the gate's StateConfig.rework_to is set
                if not sc.rework_to:
                    errors.append(
                        f"Gate '{state_name}' in workflow '{wf.name}' has no resolvable "
                        f"rework_to (no explicit rework_to and no prior agent state in path)"
                    )

        # Per-workflow reachability: walk this workflow's transition graph
        wf_entry = wf.entry_state
        wf_reachable: set[str] = set()
        if wf_entry:
            wf_reachable.add(wf_entry)
        for state_name, state_transitions in wf.transitions.items():
            for target in state_transitions.values():
                wf_reachable.add(target)
        wf_path_set = set(wf.path)
        wf_unreachable = wf_path_set - wf_reachable
        for name in wf_unreachable:
            log.warning(
                "State '%s' is unreachable in workflow '%s' "
                "(no transitions lead to it)",
                name, wf.name,
            )

    # Warn if a state in the pool is not referenced by any workflow path
    unreferenced = all_state_names - all_referenced_states
    for name in unreferenced:
        log.warning(
            "State '%s' is defined but not referenced by any workflow path", name
        )

    # Legacy mode: also run the original unreachable-states check using
    # StateConfig.transitions (the synthesized _default workflow copies them
    # verbatim, so the per-workflow check above covers this — but we keep
    # the original check for backward compatibility in case the logic diverges)
    if is_legacy:
        entry = cfg.entry_state
        reachable: set[str] = set()
        if entry:
            reachable.add(entry)
        for sc in cfg.states.values():
            for target in sc.transitions.values():
                reachable.add(target)
            if sc.rework_to:
                reachable.add(sc.rework_to)

        unreachable = all_state_names - reachable
        for name in unreachable:
            log.warning("State '%s' is unreachable (no transitions lead to it)", name)

    # Docker validation
    if cfg.docker.enabled:
        if not cfg.docker.default_image:
            errors.append("docker.enabled is true but docker.default_image is not set")
        if cfg.docker.inherit_claude_config:
            host_dir = os.path.expandvars(os.path.expanduser(cfg.docker.host_claude_dir))
            # Only warn if running locally — in DooD mode this is a host path
            # that won't exist inside the orchestrator container
            if not os.environ.get("HOST_HOME") and not Path(host_dir).exists():
                log.warning(
                    "docker.host_claude_dir '%s' does not exist — "
                    "agents may fail to authenticate",
                    host_dir,
                )
            # In DooD mode (orchestrator inside a container), the orchestrator
            # cannot write host-visible temp files without an operator-provided
            # shim. Require explicit shim config whenever a Claude Code state
            # exists — Codex-only workflows don't consume plugin config and are
            # exempt from the shim requirement.
            if os.path.exists("/.dockerenv"):
                claude_states = [
                    name for name, sc in cfg.states.items()
                    if sc.type == "agent" and sc.runner == "claude"
                ]
                if claude_states:
                    missing = []
                    if not cfg.docker.host_claude_dir_mount:
                        missing.append("docker.host_claude_dir_mount")
                    if not cfg.docker.plugin_shim_host_path:
                        missing.append("docker.plugin_shim_host_path")
                    if not cfg.docker.plugin_shim_container_path:
                        missing.append("docker.plugin_shim_container_path")
                    if missing:
                        state_list = ", ".join(sorted(claude_states))
                        errors.append(
                            "Docker-in-Docker mode detected with inherit_claude_config: true "
                            f"and Claude Code state(s) present ({state_list}), "
                            f"but required shim fields are not set: {', '.join(missing)}. "
                            "These fields are needed to rewrite plugin paths without touching "
                            "host files. See CLAUDE.md (Docker mode) for setup."
                        )
    for name, sc in cfg.states.items():
        if sc.docker_image and not cfg.docker.enabled:
            log.warning(
                "State '%s' has docker_image set but docker.enabled is false", name
            )
        if sc.skip_labels and sc.type != "gate":
            log.warning(
                "State '%s' has skip_labels but is not a gate — labels will be ignored",
                name,
            )

    # Warn if max_concurrent_agents_by_state keys don't match state names
    for state_key in cfg.agent.max_concurrent_agents_by_state:
        if state_key not in cfg.states:
            log.warning(
                "max_concurrent_agents_by_state key '%s' does not match any defined state",
                state_key,
            )

    # Validate logging
    if cfg.logging.enabled and not cfg.logging.log_dir:
        log.warning("logging.enabled is true but log_dir is not set")

    # --- Multi-repo validation (R21) ---
    errors.extend(_validate_repos(cfg))

    return errors


_VALID_CLONE_URL_SCHEMES = ("https://", "ssh://", "git@")
_CREDENTIAL_URL_RE = re.compile(r"^[^:]+://[^/]*:[^@]+@")
_PATH_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")
_RESERVED_LABEL_PREFIXES = ("workflow:", "repo:")
_RESERVED_REPO_NAME = "_default"


def _validate_repos(cfg: ServiceConfig) -> list[str]:
    """R21 validation for the repos: registry. Returns list of errors.

    Warnings are logged directly at WARNING level and do not populate the
    returned list. The synthetic ``_default`` repo (cfg.repos_synthesized)
    is exempt from integrity and path-safety checks.
    """
    errors: list[str] = []

    # Empty cfg.repos is treated as "no multi-repo configured" (equivalent to
    # legacy-synthesized). This only happens when a test builds ServiceConfig
    # directly without going through parse_workflow_file; parsed configs
    # always have at least the synthetic _default.
    if not cfg.repos:
        return errors

    # Repo entry integrity + path safety + reserved-name check
    seen_labels: dict[str, str] = {}
    default_count = 0
    for name, repo in cfg.repos.items():
        is_synthetic_default = (
            cfg.repos_synthesized and name == _RESERVED_REPO_NAME
        )

        # Path safety: registry name must match the sanitize_key regex so the
        # composite workspace key is safe for filesystem paths.
        if not _PATH_SAFE_NAME_RE.match(name):
            errors.append(
                f"Repo name '{name}' contains invalid characters "
                f"(must match [A-Za-z0-9._-])"
            )

        # Operator-authored repos may not use the reserved _default name.
        if name == _RESERVED_REPO_NAME and not cfg.repos_synthesized:
            errors.append(
                f"Repo name '_default' is reserved for the legacy synthesis "
                f"branch; rename this entry (e.g., 'default-repo')"
            )

        if is_synthetic_default:
            # Exempt from non-empty checks and duplicate-label checks.
            if repo.default:
                default_count += 1
            continue

        # Non-empty clone_url and label
        if not repo.clone_url:
            errors.append(f"Repo '{name}' has empty clone_url")
        if not repo.label:
            errors.append(f"Repo '{name}' has empty label")

        # clone_url scheme check
        if repo.clone_url:
            if repo.clone_url.startswith("file://"):
                errors.append(
                    f"Repo '{name}' clone_url uses file:// scheme "
                    f"(rejected for safety)"
                )
            elif _CREDENTIAL_URL_RE.match(repo.clone_url):
                errors.append(
                    f"Repo '{name}' clone_url contains embedded credentials "
                    f"(user:pass@host); move credentials to a git credential "
                    f"helper or an environment variable"
                )
            elif not repo.clone_url.startswith(_VALID_CLONE_URL_SCHEMES):
                errors.append(
                    f"Repo '{name}' clone_url must use https://, ssh://, or "
                    f"git@ form (got: {repo.clone_url[:40]!r})"
                )

        # Unique labels across repos (case-insensitive)
        if repo.label:
            label_lower = repo.label.lower()
            if label_lower in seen_labels:
                errors.append(
                    f"Duplicate repo label '{repo.label}' on repos "
                    f"'{seen_labels[label_lower]}' and '{name}'"
                )
            else:
                seen_labels[label_lower] = name

        if repo.default:
            default_count += 1

    # Default constraint
    if default_count > 1:
        errors.append(
            f"Multiple default repos defined ({default_count}); "
            f"at most one repo may have default: true"
        )

    # Single-repo configs must mark their one repo as default. This enforces
    # R3's "trivially defaulted" case explicitly rather than inferring it.
    non_synthetic = [
        r for n, r in cfg.repos.items()
        if not (cfg.repos_synthesized and n == _RESERVED_REPO_NAME)
    ]
    if len(non_synthetic) == 1 and not non_synthetic[0].default:
        errors.append(
            f"Repo '{non_synthetic[0].name}' is the only repo defined but is "
            f"not marked default: true; single-repo configs must mark their "
            f"sole repo as default"
        )

    # Triage requirement: multi-repo (excluding synthetic) + no default →
    # require exactly one workflow with triage=True so unlabeled tickets can
    # be routed through triage to acquire a repo:* label.
    if len(non_synthetic) > 1 and default_count == 0:
        triage_workflows = [
            wf for wf in cfg.workflows.values() if wf.triage
        ]
        if len(triage_workflows) == 0:
            errors.append(
                "Multi-repo config with no default repo requires exactly one "
                "workflow with triage: true to route unlabeled tickets "
                "(found: 0 triage workflows)"
            )
        elif len(triage_workflows) > 1:
            names = ", ".join(sorted(w.name for w in triage_workflows))
            errors.append(
                f"Exactly one workflow may have triage: true "
                f"(found {len(triage_workflows)}: {names})"
            )

    # Reserved-prefix warning: warn on operator-declared labels that
    # near-match stokowski's reserved namespaces (typo protection). Checked
    # across workflows and repos; emitted at WARNING level.
    for prefix in _RESERVED_LABEL_PREFIXES:
        near_match_typos = _near_match_prefixes(prefix)
        for wf in cfg.workflows.values():
            if wf.label and any(
                wf.label.lower().startswith(t) for t in near_match_typos
            ):
                log.warning(
                    "Workflow '%s' label '%s' looks like a near-match to "
                    "the reserved prefix '%s' — typo?",
                    wf.name, wf.label, prefix,
                )
        for repo in cfg.repos.values():
            if repo.label and any(
                repo.label.lower().startswith(t) for t in near_match_typos
            ):
                log.warning(
                    "Repo '%s' label '%s' looks like a near-match to the "
                    "reserved prefix '%s' — typo?",
                    repo.name, repo.label, prefix,
                )

    return errors


def _near_match_prefixes(prefix: str) -> list[str]:
    """Generate simple transposition/typo variants of a reserved prefix.

    Covers common typos: single-char transposition and trailing-s variants.
    Not exhaustive — only catches the obvious mistakes an operator might make.
    """
    variants: set[str] = set()
    base = prefix.rstrip(":")  # "workflow" / "repo"
    # Swap adjacent characters (one pair at a time)
    for i in range(len(base) - 1):
        swapped = list(base)
        swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
        variant = "".join(swapped) + ":"
        if variant != prefix:
            variants.add(variant)
    # Trailing-s plural (e.g., repos: instead of repo:)
    variants.add(base + "s:")
    return list(variants)
