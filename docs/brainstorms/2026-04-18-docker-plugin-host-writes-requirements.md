# Docker plugin config: eliminate host writes in DooD/DinD mode

**Status:** Ready for planning
**Date:** 2026-04-18
**Scope:** Standard

## Problem

In Docker-outside-of-Docker mode, every agent launch rewrites the operator's host `~/.claude/plugins/{installed_plugins,known_marketplaces}.json` to contain container-absolute paths (`/home/agent/.claude/...`). Once polluted, the host Claude Code CLI reports plugins as missing from the marketplace.

Commit `c3afaed` fixed the non-DooD path cleanly (host-side tempfile + `:ro` overlay, zero host writes). The DooD path still round-trips through the host file: the in-container bash fallback does `cp "$TMP" "$SRC"` where `$SRC` is the bind-mounted host file.

## Root cause

In DooD, the orchestrator container has no location that is (a) writable by the orchestrator and (b) visible to the host Docker daemon for passing to agent `docker run -v` mounts. The host's `/tmp` isn't the orchestrator's `/tmp`, and the orchestrator can't create files on the host filesystem from inside its own namespace. The current fallback papers over this by writing in-container, but the only target it can reach — `$HOME/.claude/plugins/*.json` — is the bind-mounted host file. This mechanism fundamentally cannot satisfy "no host writes."

## Principle

Stokowski must never issue writes to the operator's `~/.claude` directory to prepare container mounts. No self-healing guards, no post-exit validation, no "correct it if it gets corrupted." The code must be structurally incapable of polluting host plugin config.

## Requirements

### In-scope

1. **Non-DooD behavior unchanged.** `stokowski/docker_runner.py` already handles this correctly via host tempfile + `:ro` overlay. Do not regress.

2. **Principled DooD support via operator-configured shim.** In DooD mode, the orchestrator writes rewritten plugin configs to a host-visible shim directory it has been given explicit access to, then bind-mounts those files `:ro` into agent containers by their host path.

3. **Two new `DockerConfig` fields for DooD operation:**
   - A way for the orchestrator to *read* the host's `.claude` directory (it can't read `/Users/…/.claude` from inside its own container namespace).
   - A pair of paths identifying a shim directory: one as the orchestrator sees it (for writing), one as the host Docker daemon resolves it (for agent `-v` mounts).

4. **Delete the DooD bash fallback in `build_docker_run_args`.** The `if docker_cfg.inherit_claude_config and not plugins_prepared:` branch at `stokowski/docker_runner.py:266-287` is removed. Agents run the command directly.

5. **Fail-fast startup validation.** When DooD is detected and `inherit_claude_config: true` is configured without the shim fields, Stokowski refuses to start with an error naming the missing config fields. Silent degradation to "agents run without plugin config" is not acceptable — operators should know they're broken before they dispatch an agent.

6. **DooD detection signal.** Use presence of `/.dockerenv` as the DooD signal. Explicit config override (e.g., `docker.dood_mode: true|false`) is out of scope — the file check is reliable and standard.

7. **Backward compatibility.** Operators running Stokowski directly on the host (the existing `Mac` use case) see no config changes required. Non-DooD users' `workflow.yaml` files continue to work unchanged.

8. **Documentation.** Update `CLAUDE.md`'s "Docker mode" pitfall section to reflect the new, principled flow. Remove the inaccurate "copies and rewrites inside the agent container" description.

### Out of scope

- Decoupling `plugins/` from the broader `~/.claude` RW inherit (a larger refactor; defer).
- Changes to `.claude.json` handling (file isn't known to carry container-relative path references).
- Image-baked plugins / `inherit_claude_config: false` flow (works today; no change needed).
- Self-healing of already-polluted host files (user has cleaned theirs up once; no recurring heal needed post-fix).
- Fargate-specific storage (EFS) integration. The shim-path-pair abstraction generalizes, but the ECS/Fargate wiring is a separate piece of work.

## Success criteria

1. After the change, running an agent in DooD mode with `inherit_claude_config: true` and the new shim fields leaves the operator's host `~/.claude/plugins/*.json` byte-identical to its pre-run state across many consecutive agent dispatches.
2. Running an agent in DooD mode with `inherit_claude_config: true` and the new shim fields **unset** causes Stokowski to refuse to start, with an error message that names the missing fields and points to documentation.
3. Running Stokowski directly on macOS (non-DooD) continues to work with zero `workflow.yaml` changes.
4. The agent inside the container sees plugin config files with paths resolving to `/home/agent/.claude/...`, and Claude Code's `/doctor` reports the plugins as present.
5. `grep -R 'cp "$TMP" "$SRC"' stokowski/` returns nothing. The destructive fallback is gone.

## Notes for planning

- Tempfile cleanup for cached shim files should overwrite deterministically (same name per cache key) rather than accumulate per-launch.
- The `_plugin_file_cache` key stays `(host_claude_dir, container_home, relative_path)` — cache semantics unchanged.
- DooD-visible read path for the host `.claude` dir should be a separate field from `host_claude_dir` (which is the *host* path used in `-v` args to agents and in path-rewrite substring replacement). Conflating them breaks the rewrite.

## Open questions for planning phase

- Exact field names (e.g., `host_claude_dir_mount` vs. `claude_dir_readable_at`) — bikeshed at planning time.
- Whether `validate_config` or orchestrator startup is the right place to enforce the DooD-requires-shim check.
- Whether to warn (not fail) when DooD is detected but `inherit_claude_config: false` — the shim isn't needed in that case.
