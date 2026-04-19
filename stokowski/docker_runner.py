"""Docker container lifecycle — builds docker run commands and manages containers/volumes."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

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
    expanded = os.path.expandvars(os.path.expanduser(path))
    # Warn if variable expansion left unexpanded references
    if "$" in expanded or "${" in expanded:
        logger.warning(
            "host path %r still contains unexpanded variables after expansion: %r "
            "(is the env var set?)",
            path,
            expanded,
        )
    return expanded


_plugin_file_cache: dict[tuple[str, str, str], tuple[str, str, float]] = {}
"""Cache of (host_dir, container_home, relative_path) → (host_mount_path, orch_write_path, mtime).

host_mount_path is passed to ``docker run -v`` (must resolve on the host Docker daemon).
orch_write_path is where the orchestrator process writes the file (same filesystem as host_mount_path,
but named via the orchestrator's view of it in DooD). The two paths are identical in non-DooD.
Invalidated when source mtime changes.
"""

_PLUGIN_FILES_TO_REWRITE = (
    os.path.join("plugins", "installed_plugins.json"),
    os.path.join("plugins", "known_marketplaces.json"),
)


def _is_dood() -> bool:
    """Detect whether the orchestrator process is running inside a container."""
    return os.path.exists("/.dockerenv")


def _prepare_plugin_file(
    host_claude_dir: str,
    container_home: str,
    relative_path: str,
    *,
    read_from_dir: str = "",
    shim_host_dir: str = "",
    shim_container_dir: str = "",
) -> str | None:
    """Stage a rewritten plugin config file and return its host path for bind-mounting.

    The orchestrator reads the operator's plugin config (from ``read_from_dir`` in DooD,
    or directly from ``host_claude_dir`` in non-DooD), rewrites absolute path references
    from the host's ``.claude`` location to the agent container's, and writes the result
    to a host-visible location. The returned path is what the host Docker daemon will
    resolve when passed to ``docker run -v <path>:<target>:ro``.

    This function never writes to the operator's ``.claude`` directory. In DooD mode
    without a configured shim, it returns None — callers must treat that as a
    configuration error rather than silently proceed.

    Args:
        host_claude_dir: Host-resolvable path of the operator's ``.claude`` dir.
            Used for path-rewriting substitution and as a cache key component.
        container_home: Agent container's HOME (e.g. ``/home/agent``).
        relative_path: Path under ``.claude/`` to the plugin config file.
        read_from_dir: Orchestrator-visible path to the operator's ``.claude`` dir.
            Required in DooD; ignored (defaults to host_claude_dir) otherwise.
        shim_host_dir: Host-resolvable path of the shim directory for agent ``-v``
            mounts. Required in DooD; when empty, uses a host tempfile.
        shim_container_dir: Orchestrator-visible path of the same shim directory.
            Required in DooD; when empty, uses a host tempfile.
    """
    read_dir = read_from_dir or host_claude_dir
    read_file = os.path.join(read_dir, relative_path)
    if not os.path.isfile(read_file):
        return None

    cache_key = (host_claude_dir, container_home, relative_path)
    try:
        current_mtime = os.path.getmtime(read_file)
    except OSError:
        current_mtime = 0.0

    cached = _plugin_file_cache.get(cache_key)
    if cached:
        cached_host_path, cached_orch_path, cached_mtime = cached
        if current_mtime == cached_mtime and os.path.isfile(cached_orch_path):
            return cached_host_path

    try:
        with open(read_file, "r") as f:
            content = f.read()
    except PermissionError:
        logger.warning("Cannot read %s — skipping path rewrite for container", read_file)
        return None

    container_claude_dir = f"{container_home}/.claude"
    rewritten = content.replace(host_claude_dir, container_claude_dir)

    dood_mode = bool(shim_host_dir and shim_container_dir)
    if dood_mode:
        if not os.path.isdir(shim_container_dir):
            logger.error(
                "plugin_shim_container_path %r is not a directory inside the orchestrator. "
                "Bind-mount the shim host path into the orchestrator container at this location.",
                shim_container_dir,
            )
            return None
        safe_name = relative_path.replace(os.sep, "__")
        orch_write_path = os.path.join(shim_container_dir, f"stokowski-plugin-{safe_name}")
        host_mount_path = os.path.join(shim_host_dir, f"stokowski-plugin-{safe_name}")
        with open(orch_write_path, "w") as f:
            f.write(rewritten)
        os.chmod(orch_write_path, 0o644)
    else:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", prefix="stokowski-plugin-", suffix=".json", delete=False
        )
        tmp.write(rewritten)
        tmp.close()
        os.chmod(tmp.name, 0o644)
        orch_write_path = tmp.name
        host_mount_path = tmp.name

    # Clean up the prior cached temp (non-DooD only — DooD uses deterministic names
    # that overwrite in place).
    if cached and not dood_mode:
        old_orch_path = cached[1]
        if old_orch_path != orch_write_path:
            try:
                os.unlink(old_orch_path)
            except OSError:
                pass

    _plugin_file_cache[cache_key] = (host_mount_path, orch_write_path, current_mtime)
    return host_mount_path


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
) -> bool:
    """Remove a per-issue Docker volume. Best-effort. Returns True if removed."""
    vol = workspace_volume_name(docker_cfg, workspace_key)
    proc = await asyncio.create_subprocess_exec(
        "docker", "volume", "rm", vol,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=_DOCKER_CLI_TIMEOUT)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        logger.warning(f"Timed out removing volume {vol}, killing docker CLI process")
        proc.kill()
        return False


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
        # Mount into /home/agent (non-root user in Dockerfile.agent)
        home = "/home/agent"
        host_dir = resolve_host_path(docker_cfg.host_claude_dir)
        args.extend(["-v", f"{host_dir}:{home}/.claude"])
        # Claude Code also reads ~/.claude.json for its main config
        host_json = os.path.join(os.path.dirname(host_dir), ".claude.json")
        args.extend(["-v", f"{host_json}:{home}/.claude.json"])
        # Stage rewritten plugin config files to a host-visible location, then
        # bind-mount them :ro over the originals. This never writes to host_dir.
        # In DooD mode the orchestrator needs an operator-provided shim: a host
        # directory bind-mounted into the orchestrator container (for writing)
        # whose host path is separately known (for passing to agent -v mounts).
        # See docker_runner._prepare_plugin_file for the contract.
        read_from = resolve_host_path(docker_cfg.host_claude_dir_mount) if docker_cfg.host_claude_dir_mount else ""
        shim_host = resolve_host_path(docker_cfg.plugin_shim_host_path) if docker_cfg.plugin_shim_host_path else ""
        shim_container = resolve_host_path(docker_cfg.plugin_shim_container_path) if docker_cfg.plugin_shim_container_path else ""
        for rel_path in _PLUGIN_FILES_TO_REWRITE:
            host_mount_path = _prepare_plugin_file(
                host_dir, home, rel_path,
                read_from_dir=read_from,
                shim_host_dir=shim_host,
                shim_container_dir=shim_container,
            )
            if host_mount_path:
                target = f"{home}/.claude/{rel_path}"
                args.extend(["-v", f"{host_mount_path}:{target}:ro"])
    else:
        args.extend(["-v", f"{docker_cfg.sessions_volume}:/home/agent/.claude"])

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

    # Command runs directly. Plugin path rewriting is handled entirely host-side
    # via the :ro overlay mounts above — the agent container never rewrites
    # anything at runtime.
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
