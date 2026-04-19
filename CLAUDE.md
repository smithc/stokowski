# Stokowski

Claude Code adaptation of [OpenAI's Symphony](https://github.com/openai/symphony). Orchestrates Claude Code agents via Linear issues.

This file is the single source of truth for contributors. It covers architecture, design decisions, key behaviours, and how to work on the codebase.

---

## What it does

Stokowski is a long-running Python daemon that:
1. Polls Linear for issues in configured active states
2. Creates an isolated git-cloned workspace per issue
3. Launches Claude Code (`claude -p`) in that workspace
4. Manages multi-turn sessions via `--resume <session_id>`
5. Retries failures with exponential backoff
6. Reconciles running agents against Linear state changes
7. Exposes a live web dashboard and terminal UI

The agent prompt, runtime config, and workspace setup all live in `workflow.yaml` in the operator's directory — not in this codebase.

---

## Package structure

```
stokowski/
  config.py        workflow.yaml parser + typed config dataclasses
  linear.py        Linear GraphQL client (httpx async)
  models.py        Domain models: Issue, RunAttempt, RetryEntry
  orchestrator.py  Main poll loop, dispatch, reconciliation, retry
  prompt.py        Three-layer prompt assembly for state machine workflows
  runner.py        Claude Code CLI integration, stream-json parser
  tracking.py      State machine tracking via structured Linear comments
  workspace.py     Per-issue workspace lifecycle and hooks
  web.py           Optional FastAPI dashboard
  main.py          CLI entry point, keyboard handler
  __main__.py      Enables python -m stokowski
```

---

## Key design decisions

### Claude Code CLI instead of Codex app-server
Symphony uses Codex's JSON-RPC `app-server` protocol over stdio. Stokowski uses Claude Code's CLI:
- First turn: `claude -p "<prompt>" --output-format stream-json --verbose`
- Continuation: `claude -p "<prompt>" --resume <session_id> --output-format stream-json --verbose`

`--verbose` is required for `stream-json` to work. `session_id` is extracted from the `result` event in the NDJSON stream.

### Python + asyncio instead of Elixir/OTP
Simpler operational story — single process, no BEAM runtime, no distributed concerns. Concurrency via `asyncio.create_task`. Each agent turn is a subprocess launched with `asyncio.create_subprocess_exec`.

### No persistent database
All state lives in memory. The orchestrator recovers from restart by re-polling Linear and re-discovering active issues. Workspace directories on disk act as durable state.

### workflow.yaml as the operator contract
The operator's `workflow.yaml` defines the runtime config and state machine. Stokowski re-parses it on every poll tick — config changes take effect without restart. Both `.yaml` and legacy `.md` (YAML front matter + Jinja2 body) formats are supported. Prompt templates are now separate `.md` files referenced by path from the config.

### State machine workflow
Each workflow defines a set of internal states that map to Linear states. States have types: `agent` (runs Claude Code), `gate` (waits for human review), or `terminal` (issue complete). Transitions between states are declared explicitly in config.

**Three-layer prompt assembly:** Every agent turn's prompt is built from three layers concatenated together:
1. **Global prompt** — shared context loaded from a `.md` file (referenced by `prompts.global_prompt`)
2. **Stage prompt** — state-specific instructions loaded from the state's `prompt` path
3. **Lifecycle injection** — auto-generated section with issue metadata, transitions, rework context, and recent comments

**Gate protocol:** When an agent completes a state that transitions to a gate, Stokowski moves the issue to the gate's Linear state and posts a structured tracking comment. Humans approve or request rework via Linear state changes. On approval, Stokowski advances to the gate's `approve` transition target. On rework, it returns to the gate's `rework_to` state.

**Structured comment tracking:** State transitions and gate decisions are persisted as HTML comments on Linear issues (`<!-- stokowski:state {...} -->` and `<!-- stokowski:gate {...} -->`). These enable crash recovery and provide context for rework runs.

### Workspace isolation
Each issue gets its own directory under `workspace.root`. Agents run with `cwd` set to that directory. Workspaces persist across turns for the same session; they're deleted when the issue reaches a terminal state.

### Headless system prompt
Every first-turn launch appends a system prompt via `--append-system-prompt` that instructs Claude not to use interactive skills, slash commands, or plan mode. This prevents agents from stalling on interactive workflows.

---

## Component deep-dives

### config.py
Parses `workflow.yaml` (or legacy `.md` with front matter) into typed dataclasses:
- `TrackerConfig` — Linear endpoint, API key, project slug
- `PollingConfig` — interval
- `WorkspaceConfig` — root path (supports `~` and `$VAR` expansion)
- `HooksConfig` — shell scripts for lifecycle events + timeout (includes `on_stage_enter`)
- `ClaudeConfig` — command, permission mode, model, timeouts, system prompt
- `AgentConfig` — concurrency limits (global + per-state)
- `ServerConfig` — optional web dashboard port
- `LoggingConfig` — agent run log retention: `enabled` (default false), `log_dir` (supports `~` and `$VAR`), `max_age_days` (default 14), `max_total_size_mb` (default 500). `resolved_log_dir()` expands path variables.
- `LinearStatesConfig` — maps logical state names (`todo`, `active`, `review`, `gate_approved`, `rework`, `terminal`) to actual Linear state names. Issues in the `todo` state are picked up and automatically moved to `active` on dispatch.
- `PromptsConfig` — global prompt file reference
- `StateConfig` — a single state in the state machine: type, prompt path, linear_state key, runner, session mode, transitions, per-state overrides (model, max_turns, timeouts, hooks), gate-specific fields (rework_to, max_rework)

`ServiceConfig` provides helper methods: `entry_state` (first agent state), `active_linear_states()`, `gate_linear_states()`, `terminal_linear_states()`.

`merge_state_config(state, root_claude, root_hooks)` merges per-state overrides with root defaults — only specified fields are overridden. Returns `(ClaudeConfig, HooksConfig)`.

`parse_workflow_file()` detects format by file extension: `.yaml`/`.yml` files are parsed as pure YAML; `.md` files are split on `---` delimiters for front matter + body.

`validate_config()` checks state machine integrity: all transitions point to existing states, gates have `rework_to` and `approve` transition, at least one agent and one terminal state exist, warns about unreachable states.

`ServiceConfig.resolved_api_key()` resolves the key in priority order:
1. Literal value in YAML
2. `$VAR` reference resolved from env
3. `LINEAR_API_KEY` env var as fallback

### linear.py
Async GraphQL client over httpx. Three queries:
- `fetch_candidate_issues()` — paginated, fetches all issues in active states with full detail (labels, blockers, branch name)
- `fetch_issue_states_by_ids()` — lightweight reconciliation query, returns `{id: state_name}`
- `fetch_issues_by_states()` — used on startup cleanup, returns minimal Issue objects

Note: the reconciliation query uses `issues(filter: { id: { in: $ids } })` — not `nodes(ids:)` which doesn't exist in Linear's API.

### models.py
Three dataclasses:
- `Issue` — normalized Linear issue. `title` is required even for minimal fetches (use `title=""`).
- `RunAttempt` — per-issue runtime state: session_id, turn count, token usage, status, last message
- `RetryEntry` — retry queue entry with due time and error

### orchestrator.py
The main loop. `start()` runs until `stop()` is called:

```
while running:
    _tick()          # reconcile → fetch → dispatch
    sleep(interval)  # interruptible via asyncio.Event
```

**Dispatch logic:**
1. Issues sorted by priority (lower = higher), then created_at, then identifier
2. `_is_eligible()` checks: valid fields, active state, not already running/claimed, blockers resolved
3. Per-state concurrency limits checked against `max_concurrent_agents_by_state`
4. `_dispatch()` creates a `RunAttempt`, adds to `self.running`, spawns `_run_worker` task

**Reconciliation:** on each tick, fetches current states for all running AND gated issue IDs (`self.running | self._pending_gates`). If an issue moved to terminal state → `_kill_worker()` (kills PID + container + task) + `_cleanup_issue_state()` + remove workspace. If moved to review → `_kill_worker()`. If moved out of active states → `_kill_worker()` + `_cleanup_issue_state()`. If a gated issue is not found in Linear (deleted/archived) → `_cleanup_issue_state()`.

**Cancellation infrastructure:**
- `_kill_pid(pid)` — static method, sends SIGKILL to process group with individual kill fallback
- `_kill_worker(issue_id, reason)` — kills subprocess PID → Docker container → async task (order matters: CancelledError does not propagate to child processes)
- `_cleanup_issue_state(issue_id)` — removes all 11 per-issue tracking dict entries. Idempotent. Also used by `_transition()` terminal branch.
- `_force_cancelled` set — populated by `_reconcile()` before calling `_kill_worker()`. Checked at the top of `_on_worker_exit()` to prevent double-processing (token aggregation is not idempotent, and `_safe_transition()` could re-populate tracking dicts after cleanup).
- `_fire_and_forget(coro)` — schedules a coroutine without awaiting it, with `_background_tasks` set to prevent GC.

**Agent self-cancellation:** agents can emit `<!-- transition:cancel -->` to cleanly exit without entering a retry loop.

**Retry logic:**
- `succeeded` → schedule continuation retry in 1s (checks if more work needed)
- `failed/timed_out/stalled` → exponential backoff: `min(10000 * 2^(attempt-1), max_retry_backoff_ms)`
- `canceled` → release claim immediately

**Shutdown:** `stop()` sets `_stop_event`, kills all child PIDs via `_kill_pid()`, calls `cleanup_orphaned_containers()`, cancels async tasks. Uses bulk operations (not per-issue `_kill_worker()`) for speed.

### runner.py
`run_agent_turn()` builds CLI args, launches subprocess, streams NDJSON output. Sets `attempt.pid` after subprocess creation for targeted kill.

**Scope restriction guardrail:** `build_claude_args()` accepts `issue_identifier` and interpolates a prohibition into the headless system prompt on first turns. The text uses a read/write split: agents MAY read other issues for context but MUST NOT write to them. The `SCOPE_RESTRICTION_SYSTEM` constant uses `str.format()` (not Jinja2 — `_SilentUndefined` would silently drop the identifier, removing the guardrail).

**Agent run log capture:** When `log_path` is provided, raw stdout bytes are written to a file during `read_stream()`. The file handle is opened before the `asyncio.wait()` block and closed in a `finally` — this ensures cleanup on all exit paths including `CancelledError`. Write failures are silently swallowed (best-effort). Both `run_agent_turn()` (NDJSON) and `run_codex_turn()` (plain text) support log capture via the same `log_path` parameter.

**PID tracking:** `on_pid` callback registers/unregisters child PIDs with the orchestrator for clean shutdown.

**Stall detection:** background `stall_monitor()` task checks time since last output. Kills process if `stall_timeout_ms` exceeded.

**Turn timeout:** `asyncio.wait()` with `turn_timeout_ms` as overall deadline.

**Event processing** (`_process_event`):
- `result` event → extracts `session_id`, token usage, result text
- `assistant` event → extracts last message for display
- `tool_use` event → updates last message with tool name

### workspace.py
`ensure_workspace()` creates the directory if needed, runs `after_create` hook on first creation.
`remove_workspace()` runs `before_remove` hook, then deletes the directory.
`run_hook()` executes shell scripts via `asyncio.create_subprocess_shell` with timeout.

Workspace key is the sanitized issue identifier: only `[A-Za-z0-9._-]` characters.

### web.py
Optional FastAPI app returned by `create_app(orch)`. Routes:
- `GET /` — HTML dashboard (IBM Plex Mono font, dark theme, amber accents)
- `GET /api/v1/state` — full JSON snapshot from `orch.get_state_snapshot()`
- `GET /api/v1/{issue_identifier}` — single issue state
- `POST /api/v1/refresh` — triggers `orch._tick()` immediately

Dashboard JS polls `/api/v1/state` every 3s and updates the DOM without page reload.

Uvicorn is started as an `asyncio.create_task` with `install_signal_handlers` monkey-patched to a no-op to prevent it hijacking SIGINT/SIGTERM. On shutdown, `server.should_exit = True` is set and the task is awaited with a 2s timeout.

### main.py
CLI entry point (`cli()`) and keyboard handler.

**`KeyboardHandler`** runs in a daemon background thread using `tty.setcbreak()` (not `setraw` — `setraw` disables `OPOST` output processing which causes diagonal log output). Uses `select.select()` with 100ms timeout for non-blocking key reads. Restores terminal state in `finally`.

**`_make_footer()`** builds the Rich `Text` status line shown at bottom of terminal via `Live`.

**`check_for_updates()`** hits the GitHub releases API (`/repos/Sugar-Coffee/stokowski/releases/latest`) via httpx, compares the latest tag against the installed `__version__`, and sets `_update_message` if a newer version exists. Best-effort — all exceptions are silently swallowed.

**`_force_kill_children()`** uses `pgrep -f "claude.*-p.*--output-format.*stream-json"` as a last-resort cleanup on `KeyboardInterrupt`.

**`_load_dotenv()`** reads `.env` from cwd on startup — supports `KEY=value` format, ignores comments and blank lines. The project-local `.env` takes precedence over the shell environment (uses direct assignment, overrides existing env vars).

### prompt.py
Three-layer prompt assembly for state machine workflows. Main entry point is `assemble_prompt()`.

**`load_prompt_file(path, workflow_dir)`** resolves a prompt file path (absolute or relative to workflow dir) and returns its contents.

**`render_template(template_str, context)`** renders a Jinja2 template with `_SilentUndefined` — missing variables render as empty strings instead of raising errors.

**`build_template_context(issue, state_name, run, attempt, last_run_at)`** builds the flat dict used for Jinja2 rendering. Includes: `issue_id`, `issue_identifier`, `issue_title`, `issue_description`, `issue_url`, `issue_priority`, `issue_state`, `issue_branch`, `issue_labels`, `state_name`, `run`, `attempt`, `last_run_at`.

**`build_lifecycle_section()`** generates the auto-injected lifecycle section appended to every prompt. Includes issue metadata, **scope restriction guardrail** (read/write split — agents may read other issues but must not write to them), rework context with review comments, recent activity, available transitions, and completion instructions. Clearly demarcated with HTML comments.

**`assemble_prompt()`** orchestrates the three layers: loads and renders global prompt, loads and renders stage prompt, generates lifecycle section, joins with double newlines.

### tracking.py
State machine tracking via structured Linear comments:
- `make_state_comment(state, run)` — builds state entry comment with hidden JSON (`<!-- stokowski:state {...} -->`) + human-readable text
- `make_gate_comment(state, status, prompt, rework_to, run)` — builds gate status comment (waiting/approved/rework/escalated)
- `parse_latest_tracking(comments)` — scans comments (oldest-first) to find latest state or gate tracking entry for crash recovery
- `get_last_tracking_timestamp(comments)` — finds the timestamp of the latest tracking comment
- `get_comments_since(comments, since_timestamp)` — filters to non-tracking comments after a given timestamp (used to gather review feedback for rework runs)

---

## Data flow: issue dispatch to PR

```
workflow.yaml parsed → states + config loaded
    → Linear poll → Issue fetched → state resolved from tracking comments
    → _dispatch() called
        → RunAttempt created in self.running
        → _run_worker() task spawned
            → ensure_workspace() → after_create hook (git clone, npm install, etc.)
            → assemble_prompt() → 3 layers: global + stage + lifecycle
            → run_agent_turn() called in loop (up to max_turns)
                → build_claude_args() → claude -p subprocess
                → NDJSON streamed: tool_use events, assistant messages, result
                → session_id captured for next turn
            → _on_worker_exit() called
                → state transition on success → tracking comment posted
                → tokens/timing aggregated
                → retry or continuation scheduled
```

The agent itself handles: moving Linear state, posting comments, creating branches, opening PRs via `gh pr create`, linking PR to issue. Stokowski doesn't do any of that — it's the scheduler, not the agent.

---

## Stream-json event format

Claude Code emits NDJSON on stdout when run with `--output-format stream-json --verbose`. Key event types:

```json
{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
{"type": "tool_use", "name": "Bash", "input": {"command": "..."}}
{"type": "result", "session_id": "uuid", "usage": {"input_tokens": 1234, "output_tokens": 456, "total_tokens": 1690}, "result": "final message text"}
```

Exit code 0 = success. Non-zero = failure (stderr captured for error message).

---

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[web]"

# Validate config without dispatching agents
stokowski --dry-run

# Run with verbose logging
stokowski -v

# Run with web dashboard
stokowski --port 4200
```

There are no automated tests beyond `--dry-run`. The system is best verified by running against a real Linear project with a test ticket.

---

## Contributing

### Adding a new tracker (not Linear)
1. Add a client in a new file (e.g., `github_issues.py`) implementing the same three methods as `LinearClient`
2. Add the new tracker kind to `config.py` parsing
3. Update `orchestrator.py` to instantiate the right client based on `cfg.tracker.kind`
4. Update `validate_config()` to handle the new kind

### Adding config fields
1. Add the field to the relevant dataclass in `config.py`
2. Parse it in `parse_workflow_file()`
3. Use it wherever needed
4. Update `WORKFLOW.example.md` and the README config reference

### Changing the web dashboard
`web.py` is self-contained. The HTML/CSS/JS is inline in the `HTML` constant. The dashboard is intentionally dependency-free on the frontend — no build step, no npm.

### Common pitfalls
- **`tty.setraw` vs `tty.setcbreak`**: Don't switch back to `setraw`. It disables `OPOST` output processing and causes Rich log lines to render diagonally (no carriage return on newlines).
- **`Issue(title=...)` is required**: Minimal Issue constructors (in `linear.py` `fetch_issues_by_states` and the `orchestrator.py` state-check default) must pass `title=""` — it's a required positional field.
- **`--verbose` with stream-json**: Claude Code requires `--verbose` when using `--output-format stream-json`. Without it you get an error.
- **Linear project slug**: The `project_slug` is the hex `slugId` from the project URL, not the human-readable name. These look like `abc123def456`.
- **Uvicorn signal handlers**: Must be monkey-patched (`server.install_signal_handlers = lambda: None`) before calling `serve()`, otherwise uvicorn hijacks SIGINT.
- **workflow.yaml is pure YAML**: No markdown front matter. The legacy `.md` format with `---` delimiters is still supported but `.yaml` is the canonical format.
- **Prompt files use Jinja2 with silent undefined**: Missing variables become empty strings rather than raising errors. This is intentional — not all variables are available in every context.
- **Docker mode: Claude Code auth**: Containers cannot use interactive OAuth login. Use either `ANTHROPIC_API_KEY` (API plan) or `CLAUDE_CODE_OAUTH_TOKEN` (Pro/Max users). Generate an OAuth token via `claude setup-token` in your terminal, then add it to `.env` and list it in `docker.extra_env`. Without one of these, agents fail with "Not logged in".
- **Docker mode: agent runs as non-root**: The `Dockerfile.agent` creates a non-root `agent` user. Claude Code refuses `--dangerously-skip-permissions` as root. Volume mounts target `/home/agent/` not `/root/`.
- **Docker mode: `~/.claude.json` must also be mounted**: Claude Code stores its main config at `~/.claude.json` (a file in the home directory), separate from the `~/.claude/` directory. Both must be mounted for auth to work with `inherit_claude_config: true`.
- **Docker mode: uvicorn binds `0.0.0.0` in containers**: On macOS Docker Desktop, `127.0.0.1` inside a container isn't reachable from the host. The web dashboard auto-detects non-TTY mode and binds to `0.0.0.0`.
- **Docker mode: plugin config files are rewritten for containers**: Claude Code discovers plugins primarily through `known_marketplaces.json` (`installLocation` fields), with `installed_plugins.json` as secondary metadata. Both files store absolute host paths and are typically mode `0600`. `_prepare_plugin_file()` in `docker_runner.py` reads each file, rewrites paths to container equivalents, writes a `0644` copy to a host-visible location, and bind-mounts it read-only over the original in the agent container. **The operator's `~/.claude` directory is never written to for this purpose** — the rewrite always stages to a separate location.
- **Docker mode: DooD/DinD requires explicit shim config**: When the orchestrator itself runs in a container, it cannot see the host's `.claude` directory and has no host-visible `/tmp` to stage rewritten plugin files. With `inherit_claude_config: true`, operators must provide three `docker` config fields: `host_claude_dir_mount` (the orchestrator's view of the host `.claude` dir — bind-mount the host path here read-only), `plugin_shim_host_path` (a host-resolvable directory for staging rewrites), and `plugin_shim_container_path` (the orchestrator's view of that same shim — bind-mount the host shim path here read-write). Stokowski refuses to start without these when DooD is detected (`/.dockerenv` present). There is no fallback — prior versions wrote through the bind-mount to the host plugin files, which silently polluted them.
- **Agent scope guardrails**: Agents receive a scope restriction in both the system prompt (first turn) and lifecycle section (every turn) prohibiting writes to other Linear issues. This is a probabilistic guardrail, not hard enforcement. For hard enforcement, operators can use `permission_mode: allowedTools` to exclude Linear MCP tools — but this also blocks agents from managing their own ticket (posting comments, moving state).
- **`STOKOWSKI_ISSUE_IDENTIFIER` env var**: Set per-dispatch in `_run_worker()`, not in `ServiceConfig`. Informational only — useful for hooks that need to know which issue they service. Not a security boundary.
- **`_cleanup_issue_state()` must stay in sync with `__init__`**: Any new per-issue tracking dict added to `Orchestrator.__init__` must also be added to `_cleanup_issue_state()`. Failure to do so causes memory leaks and stale state on cancellation.
- **Agent run logs**: When `logging.enabled` is true, raw agent stdout is captured to `{log_dir}/{issue_identifier}/` as `.ndjson` (Claude Code) or `.log` (Codex) files. To debug an agent run: `cat {log_dir}/SMI-14/20260324T041500Z-turn-1.ndjson | jq .` Logs survive workspace cleanup — their lifetime is controlled by `max_age_days` and `max_total_size_mb`. Retention cleanup runs at startup and after each worker exit. Log writes are best-effort — failures do not affect agent execution.
