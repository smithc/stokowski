"""Workspace management - create, reuse, and clean per-issue workspaces."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import DockerConfig, HooksConfig

logger = logging.getLogger("stokowski.workspace")


def sanitize_key(identifier: str) -> str:
    """Replace non-safe chars with underscore for directory name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", identifier)


def compose_workspace_key(issue_identifier: str, repo_name: str) -> str:
    """Build a path-safe composite workspace key for an (issue, repo) pair.

    Both components are passed through ``sanitize_key`` so the final key
    is safe to use as a filesystem subdirectory, a Docker volume name
    component, etc. The shape is ``{issue}-{repo}``.

    For legacy 1:1 configs, the synthetic ``_default`` repo produces keys
    like ``SMI-14-_default``. v1 single-repo-per-ticket means this is a
    one-to-one rename of the prior flat ``{issue}`` layout; v2 multi-repo
    scope will reuse the same shape without modification.
    """
    return f"{sanitize_key(issue_identifier)}-{sanitize_key(repo_name)}"


@dataclass
class WorkspaceResult:
    path: Path
    workspace_key: str
    created_now: bool


async def run_hook(
    script: str,
    cwd: Path,
    timeout_ms: int,
    label: str,
    docker_cfg: DockerConfig | None = None,
    docker_image: str = "",
    workspace_key: str = "",
    force_local: bool = False,
) -> bool:
    """Run a shell hook script in the workspace directory. Returns True on success."""
    if docker_cfg and docker_cfg.enabled and not force_local:
        from .docker_runner import build_docker_run_args

        logger.info(f"hook={label} [docker] image={docker_image}")
        hook_container_name = f"stokowski-hook-{workspace_key}-{label}".lower()
        inner_cmd = ["sh", "-c", script]
        args = build_docker_run_args(
            docker_cfg=docker_cfg,
            image=docker_image,
            command=inner_cmd,
            workspace_key=workspace_key,
            env={
                var: os.environ[var]
                for var in docker_cfg.extra_env
                if var in os.environ
            },
            container_name=hook_container_name,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_ms / 1000
            )
            if proc.returncode != 0:
                logger.error(
                    f"hook={label} [docker] failed rc={proc.returncode} stderr={stderr.decode()[:500]}"
                )
                return False
            return True
        except asyncio.TimeoutError:
            logger.error(f"hook={label} [docker] timed out after {timeout_ms}ms")
            proc.kill()
            from .docker_runner import kill_container
            await kill_container(hook_container_name)
            return False
        except Exception as e:
            logger.error(f"hook={label} [docker] error: {e}")
            return False

    logger.info(f"hook={label} cwd={cwd}")
    try:
        proc = await asyncio.create_subprocess_shell(
            script,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_ms / 1000
        )
        if proc.returncode != 0:
            logger.error(
                f"hook={label} failed rc={proc.returncode} stderr={stderr.decode()[:500]}"
            )
            return False
        return True
    except asyncio.TimeoutError:
        logger.error(f"hook={label} timed out after {timeout_ms}ms")
        proc.kill()
        return False
    except Exception as e:
        logger.error(f"hook={label} error: {e}")
        return False


async def ensure_workspace(
    workspace_root: Path,
    issue_identifier: str,
    repo_name: str,
    hooks: HooksConfig,
    docker_cfg: DockerConfig | None = None,
    docker_image: str = "",
) -> WorkspaceResult:
    """Create or reuse a workspace for an (issue, repo) pair.

    v1 uses a composite workspace key ``{issue}-{repo}``. Legacy 1:1 configs
    pass ``repo_name='_default'`` (the synthetic fallback) and get a path
    like ``{workspace_root}/SMI-14-_default``.
    """
    key = compose_workspace_key(issue_identifier, repo_name)
    ws_path = workspace_root / key

    if docker_cfg and docker_cfg.enabled:
        from .docker_runner import create_workspace_volume, workspace_volume_name

        vol_name = workspace_volume_name(docker_cfg, key)
        # Check if volume exists by inspecting it
        check = await asyncio.create_subprocess_exec(
            "docker", "volume", "inspect", vol_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(check.wait(), timeout=30)
        created_now = check.returncode != 0
        if created_now:
            await create_workspace_volume(docker_cfg, key)

        # Still construct ws_path for tracking purposes (orchestrator uses it)
        ws_path.mkdir(parents=True, exist_ok=True)

        if created_now and hooks.after_create:
            ok = await run_hook(
                hooks.after_create, ws_path, hooks.timeout_ms, "after_create",
                docker_cfg=docker_cfg, docker_image=docker_image, workspace_key=key,
            )
            if not ok:
                from .docker_runner import remove_workspace_volume
                await remove_workspace_volume(docker_cfg, key)
                shutil.rmtree(ws_path, ignore_errors=True)
                raise RuntimeError(f"after_create hook failed for {issue_identifier}")

        return WorkspaceResult(path=ws_path, workspace_key=key, created_now=created_now)

    # Non-Docker path
    # Safety: workspace must be under root
    ws_abs = ws_path.resolve()
    root_abs = workspace_root.resolve()
    if not ws_abs.is_relative_to(root_abs):
        raise ValueError(f"Workspace path {ws_abs} escapes root {root_abs}")

    created_now = not ws_path.exists()
    ws_path.mkdir(parents=True, exist_ok=True)

    if created_now and hooks.after_create:
        ok = await run_hook(hooks.after_create, ws_path, hooks.timeout_ms, "after_create")
        if not ok:
            # Clean up failed workspace
            shutil.rmtree(ws_path, ignore_errors=True)
            raise RuntimeError(f"after_create hook failed for {issue_identifier}")

    return WorkspaceResult(path=ws_path, workspace_key=key, created_now=created_now)


async def remove_workspace(
    workspace_root: Path,
    issue_identifier: str,
    repo_name: str,
    hooks: HooksConfig,
    docker_cfg: DockerConfig | None = None,
) -> None:
    """Remove a workspace directory for a terminal (issue, repo) pair."""
    key = compose_workspace_key(issue_identifier, repo_name)
    ws_path = workspace_root / key

    if docker_cfg and docker_cfg.enabled:
        from .docker_runner import remove_workspace_volume

        # before_remove runs locally (R20)
        if hooks.before_remove:
            await run_hook(hooks.before_remove, ws_path, hooks.timeout_ms, "before_remove", force_local=True)
        removed = await remove_workspace_volume(docker_cfg, key)
        if removed:
            logger.info(f"Removing workspace volume issue={issue_identifier} key={key}")
        # Also clean up the local tracking directory if it exists
        if ws_path.exists():
            shutil.rmtree(ws_path, ignore_errors=True)
        return

    if not ws_path.exists():
        return

    if hooks.before_remove:
        await run_hook(hooks.before_remove, ws_path, hooks.timeout_ms, "before_remove")

    logger.info(f"Removing workspace issue={issue_identifier} path={ws_path}")
    shutil.rmtree(ws_path, ignore_errors=True)
