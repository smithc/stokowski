"""Docker container lifecycle — builds docker run commands and manages containers/volumes."""

from __future__ import annotations

import asyncio
import logging
import os

from .config import DockerConfig
from .workspace import sanitize_key

logger = logging.getLogger("stokowski.docker_runner")

_DOCKER_CLI_TIMEOUT = 30  # seconds
_DOCKER_PULL_TIMEOUT = 300  # 5 minutes


def resolve_host_path(path: str) -> str:
    """Expand ~ and $VAR in host paths for Docker -v flags.

    Does NOT call Path.resolve() — in DooD mode the orchestrator runs
    inside a container where host paths don't exist on the local
    filesystem.  The Docker daemon resolves the path on the host.
    """
    return os.path.expandvars(os.path.expanduser(path))


def workspace_volume_name(docker_cfg: DockerConfig, workspace_key: str) -> str:
    """Return the per-issue Docker volume name."""
    return f"{docker_cfg.volume_prefix}-{workspace_key}".lower()


async def create_workspace_volume(
    docker_cfg: DockerConfig, workspace_key: str
) -> str:
    """Create a per-issue Docker volume if it doesn't exist. Returns volume name."""
    vol = workspace_volume_name(docker_cfg, workspace_key)
    proc = await asyncio.create_subprocess_exec(
        "docker", "volume", "create", "--label", "stokowski=true", vol,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_CLI_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Timed out creating volume {vol} after {_DOCKER_CLI_TIMEOUT}s")
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create volume {vol}: {stderr.decode()[:200]}")
    return vol


async def remove_workspace_volume(
    docker_cfg: DockerConfig, workspace_key: str
) -> None:
    """Remove a per-issue Docker volume. Best-effort."""
    vol = workspace_volume_name(docker_cfg, workspace_key)
    proc = await asyncio.create_subprocess_exec(
        "docker", "volume", "rm", vol,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=_DOCKER_CLI_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"Timed out removing volume {vol}, killing docker CLI process")
        proc.kill()


async def cleanup_orphaned_volumes(
    docker_cfg: DockerConfig, active_keys: set[str]
) -> int:
    """Remove workspace volumes not associated with active issues."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "volume", "ls", "-q", "--filter", "label=stokowski=true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_CLI_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Timed out listing Docker volumes, killing docker CLI process")
        proc.kill()
        return 0
    count = 0
    prefix = docker_cfg.volume_prefix.lower()
    for vol_name in stdout.decode().strip().split("\n"):
        vol_name = vol_name.strip()
        if not vol_name or not vol_name.startswith(prefix):
            continue
        # Extract key from volume name
        key = vol_name[len(prefix) + 1:]  # strip "{prefix}-"
        if key not in active_keys:
            await remove_workspace_volume(docker_cfg, key)
            count += 1
    return count


def build_docker_run_args(
    docker_cfg: DockerConfig,
    image: str,
    command: list[str],
    workspace_key: str,
    env: dict[str, str],
    container_name: str | None = None,
) -> list[str]:
    """Build docker run CLI args wrapping an inner command."""
    args = ["docker", "run", "--rm", "-i"]

    if docker_cfg.init:
        args.append("--init")

    # Container identity
    if container_name:
        args.extend(["--name", container_name])
    args.extend(["--label", "stokowski=true"])

    # Host networking
    args.extend(["--network", "host"])

    # Per-issue workspace volume — full isolation, each agent only sees /workspace
    vol = workspace_volume_name(docker_cfg, workspace_key)
    args.extend(["-v", f"{vol}:/workspace", "-w", "/workspace"])

    # Claude config — either inherit from host or use sessions volume
    if docker_cfg.inherit_claude_config:
        # Read-write mount: agents can write session data for --resume support.
        # This means agents can also modify host Claude config — accepted tradeoff
        # for inherit mode. Use inherit_claude_config: false for full isolation.
        host_dir = resolve_host_path(docker_cfg.host_claude_dir)
        args.extend(["-v", f"{host_dir}:/root/.claude"])
    else:
        args.extend(["-v", f"{docker_cfg.sessions_volume}:/root/.claude"])

    # Operator-declared extra volumes
    for v in docker_cfg.extra_volumes:
        parts = v.split(":", 1)
        if len(parts) >= 2:
            expanded = resolve_host_path(parts[0])
            args.extend(["-v", f"{expanded}:{parts[1]}"])
        else:
            args.extend(["-v", v])

    # Environment variables
    for key, val in env.items():
        args.extend(["-e", f"{key}={val}"])

    # Image
    args.append(image)

    # Inner command
    args.extend(command)

    return args


def container_name_for(
    issue_identifier: str, turn: int, attempt: int | None
) -> str:
    """Generate deterministic container name."""
    key = sanitize_key(issue_identifier)
    name = f"stokowski-{key}-t{turn}"
    if attempt is not None:
        name += f"-a{attempt}"
    return name.lower()


async def kill_container(name: str) -> None:
    """Kill a running container by name. Best-effort, no error on not-found."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "kill", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning(f"Timed out killing container {name}, killing docker CLI process")
        proc.kill()


async def cleanup_orphaned_containers() -> int:
    """Find and kill orphaned stokowski containers. Returns count killed."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", "label=stokowski=true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_CLI_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Timed out listing Docker containers, killing docker CLI process")
        proc.kill()
        return 0
    container_ids = stdout.decode().strip().split("\n")
    count = 0
    for cid in container_ids:
        if cid.strip():
            await kill_container(cid.strip())
            count += 1
    return count


async def check_docker_available() -> tuple[bool, str]:
    """Check if Docker daemon is reachable. Returns (ok, message)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_CLI_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                f"Timed out checking Docker availability after {_DOCKER_CLI_TIMEOUT}s — daemon may be hung"
            )
        if proc.returncode == 0:
            return True, "Docker daemon reachable"
        return False, f"Docker daemon not reachable: {stderr.decode()[:200]}"
    except FileNotFoundError:
        return False, "Docker CLI not found in PATH"


async def pull_image(image: str) -> bool:
    """Pull a Docker image. Returns True on success."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "pull", image,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_PULL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"Timed out pulling image {image} after {_DOCKER_PULL_TIMEOUT}s")
        proc.kill()
        return False
    return proc.returncode == 0
