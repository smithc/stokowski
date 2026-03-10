"""Workflow loader and typed configuration."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class TrackerConfig:
    kind: str = "linear"
    endpoint: str = "https://api.linear.app/graphql"
    api_key: str = ""
    project_slug: str = ""
    active_states: list[str] = field(default_factory=lambda: ["Todo", "In Progress"])
    terminal_states: list[str] = field(
        default_factory=lambda: ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
    )
    gate_states: list[str] = field(default_factory=lambda: ["Awaiting Gate"])
    gate_approved_state: str = "Gate Approved"
    rework_state: str = "Rework"


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
class ServerConfig:
    port: int | None = None


@dataclass
class GateConfig:
    """Human gate checkpoint configuration."""
    rework_to: str = ""
    prompt: str = ""


@dataclass
class PipelineConfig:
    """Ordered pipeline of stages and gates."""
    stages: list[str] = field(default_factory=list)
    gates: dict[str, GateConfig] = field(default_factory=dict)


@dataclass
class StageConfig:
    """Per-stage overrides and prompt template."""
    name: str = ""
    runner: str = "claude"
    model: str | None = None
    max_turns: int | None = None
    turn_timeout_ms: int | None = None
    stall_timeout_ms: int | None = None
    session: str = "inherit"
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    append_system_prompt: str | None = None
    hooks: HooksConfig | None = None
    prompt_template: str = ""


@dataclass
class WorkflowDefinition:
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
    pipeline: PipelineConfig | None = None

    def resolved_api_key(self) -> str:
        key = self.tracker.api_key
        if not key:
            return os.environ.get("LINEAR_API_KEY", "")
        if key.startswith("$"):
            return os.environ.get(key[1:], "")
        return key


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


def parse_stage_file(path: Path) -> StageConfig:
    """Parse a stage .md file into StageConfig."""
    content = path.read_text()
    config_raw: dict[str, Any] = {}
    prompt_body = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            config_raw = yaml.safe_load(parts[1]) or {}
            prompt_body = parts[2]

    if not isinstance(config_raw, dict):
        config_raw = {}

    h = config_raw.get("hooks", {}) or {}
    hooks = None
    if h:
        hooks = HooksConfig(
            after_create=h.get("after_create"),
            before_run=h.get("before_run"),
            after_run=h.get("after_run"),
            before_remove=h.get("before_remove"),
            timeout_ms=_coerce_int(h.get("timeout_ms"), 60_000),
        )

    allowed = config_raw.get("allowed_tools")

    return StageConfig(
        name=path.stem,
        runner=str(config_raw.get("runner", "claude")),
        model=config_raw.get("model"),
        max_turns=config_raw.get("max_turns"),
        turn_timeout_ms=config_raw.get("turn_timeout_ms"),
        stall_timeout_ms=config_raw.get("stall_timeout_ms"),
        session=str(config_raw.get("session", "inherit")),
        permission_mode=config_raw.get("permission_mode"),
        allowed_tools=_coerce_list(allowed) if allowed is not None else None,
        append_system_prompt=config_raw.get("append_system_prompt"),
        hooks=hooks,
        prompt_template=prompt_body.strip(),
    )


def load_stage_configs(
    workflow_path: Path, pipeline: PipelineConfig
) -> dict[str, StageConfig]:
    """Load all stage files referenced by the pipeline. Returns {stage_name: StageConfig}."""
    stages_dir = workflow_path.parent / "stages"
    configs: dict[str, StageConfig] = {}

    for stage_name in pipeline.stages:
        if stage_name.startswith("gate:"):
            continue
        stage_file = stages_dir / f"{stage_name}.md"
        if not stage_file.exists():
            raise FileNotFoundError(
                f"Stage file not found: {stage_file} (referenced in pipeline)"
            )
        configs[stage_name] = parse_stage_file(stage_file)

    return configs


def merge_stage_config(
    stage: StageConfig, root_claude: ClaudeConfig, root_hooks: HooksConfig
) -> tuple[ClaudeConfig, HooksConfig]:
    """Merge stage overrides with root defaults. Returns (claude_cfg, hooks_cfg)."""
    claude = ClaudeConfig(
        command=root_claude.command,
        permission_mode=stage.permission_mode or root_claude.permission_mode,
        allowed_tools=stage.allowed_tools if stage.allowed_tools is not None else root_claude.allowed_tools,
        model=stage.model or root_claude.model,
        max_turns=stage.max_turns if stage.max_turns is not None else root_claude.max_turns,
        turn_timeout_ms=stage.turn_timeout_ms if stage.turn_timeout_ms is not None else root_claude.turn_timeout_ms,
        stall_timeout_ms=stage.stall_timeout_ms if stage.stall_timeout_ms is not None else root_claude.stall_timeout_ms,
        append_system_prompt=stage.append_system_prompt or root_claude.append_system_prompt,
    )
    hooks = stage.hooks if stage.hooks is not None else root_hooks
    return claude, hooks


def parse_workflow_file(path: str | Path) -> WorkflowDefinition:
    """Parse a WORKFLOW.md file into config + prompt template."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Workflow file not found: {path}")

    content = path.read_text()
    config_raw: dict[str, Any] = {}
    prompt_body = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            config_raw = yaml.safe_load(parts[1]) or {}
            prompt_body = parts[2]

    if not isinstance(config_raw, dict):
        raise ValueError("WORKFLOW.md front matter must be a YAML mapping")

    prompt_template = prompt_body.strip()

    # Parse tracker
    t = config_raw.get("tracker", {}) or {}
    tracker = TrackerConfig(
        kind=str(t.get("kind", "linear")),
        endpoint=str(t.get("endpoint", "https://api.linear.app/graphql")),
        api_key=str(t.get("api_key", "")),
        project_slug=str(t.get("project_slug", "")),
        active_states=_coerce_list(t.get("active_states")) or ["Todo", "In Progress"],
        terminal_states=_coerce_list(t.get("terminal_states"))
        or ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"],
        gate_states=_coerce_list(t.get("gate_states")) or ["Awaiting Gate"],
        gate_approved_state=str(t.get("gate_approved_state", "Gate Approved")),
        rework_state=str(t.get("rework_state", "Rework")),
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
        timeout_ms=_coerce_int(h.get("timeout_ms"), 60_000),
    )

    # Parse claude (replaces codex)
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

    # Parse pipeline (optional - enables staged workflows)
    pipeline = None
    pl = config_raw.get("pipeline", {}) or {}
    if pl and pl.get("stages"):
        gates_raw = pl.get("gates", {}) or {}
        gates = {}
        for gate_name, gate_data in gates_raw.items():
            gd = gate_data or {}
            gates[gate_name] = GateConfig(
                rework_to=str(gd.get("rework_to", "")),
                prompt=str(gd.get("prompt", "")),
            )
        pipeline = PipelineConfig(
            stages=_coerce_list(pl.get("stages")),
            gates=gates,
        )

    cfg = ServiceConfig(
        tracker=tracker,
        polling=polling,
        workspace=workspace,
        hooks=hooks,
        claude=claude,
        agent=agent,
        server=server,
        pipeline=pipeline,
    )

    return WorkflowDefinition(config=cfg, prompt_template=prompt_template)


def validate_config(cfg: ServiceConfig) -> list[str]:
    """Validate config for dispatch readiness. Returns list of errors."""
    errors = []
    if cfg.tracker.kind != "linear":
        errors.append(f"Unsupported tracker kind: {cfg.tracker.kind}")
    if not cfg.resolved_api_key():
        errors.append("Missing tracker API key (set LINEAR_API_KEY or tracker.api_key)")
    if not cfg.tracker.project_slug:
        errors.append("Missing tracker.project_slug")
    return errors
