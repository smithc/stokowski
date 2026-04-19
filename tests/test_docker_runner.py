"""Tests for docker_runner plugin-config staging + fail-fast behavior.

Covers the 2026-04-19 fail-fast fix: Claude-Code-only scoping of plugin
config preparation, raise-on-missing-shim in DooD mode, and scoped startup
validation that only fires when a Claude Code state is configured.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from stokowski.config import (
    DockerConfig,
    ServiceConfig,
    StateConfig,
    TrackerConfig,
    WorkflowConfig,
    validate_config,
)
from stokowski.docker_runner import (
    _PLUGIN_FILES_TO_REWRITE,
    _plugin_file_cache,
    _prepare_plugin_file,
    build_docker_run_args,
)


@pytest.fixture(autouse=True)
def _clear_plugin_cache():
    """Ensure per-test isolation — the module-level cache would otherwise bleed between tests."""
    _plugin_file_cache.clear()
    yield
    _plugin_file_cache.clear()


@contextmanager
def _fake_dockerenv(present: bool):
    """Toggle /.dockerenv detection for validation tests."""
    real = os.path.exists

    def fake(path):
        if path == "/.dockerenv":
            return present
        return real(path)

    with patch("stokowski.config.os.path.exists", side_effect=fake):
        yield


def _write_plugin_fixture(host_dir: str) -> None:
    """Populate a fake host .claude dir with both plugin files."""
    os.makedirs(os.path.join(host_dir, "plugins"), exist_ok=True)
    for rel in _PLUGIN_FILES_TO_REWRITE:
        with open(os.path.join(host_dir, rel), "w") as f:
            json.dump({"installPath": host_dir + "/cache/x"}, f)
    # Also need a .claude.json sibling for build_docker_run_args to mount
    parent = os.path.dirname(host_dir)
    basename = os.path.basename(host_dir)
    with open(os.path.join(parent, basename + "_claude.json"), "w") as f:
        f.write("{}")


def _minimal_service_config(states: dict[str, StateConfig], docker: DockerConfig) -> ServiceConfig:
    path = list(states.keys())
    wf = WorkflowConfig(
        name="_default",
        default=True,
        path=path,
        entry_state=path[0],
        terminal_state="terminal",
        transitions={},
    )
    return ServiceConfig(
        tracker=TrackerConfig(
            kind="linear", endpoint="x", api_key="k", project_slug="s"
        ),
        states=states,
        workflows={"_default": wf},
        docker=docker,
    )


# -----------------------------------------------------------------------------
# Unit 1: build_docker_run_args respects needs_plugin_config
# -----------------------------------------------------------------------------


def test_build_args_without_plugin_config_skips_plugin_mounts(tmp_path):
    """Codex / hook dispatches (default needs_plugin_config=False) never add plugin :ro mounts."""
    host_dir = str(tmp_path / "claude")
    _write_plugin_fixture(host_dir)

    dk = DockerConfig(
        enabled=True,
        default_image="img",
        inherit_claude_config=True,
        host_claude_dir=host_dir,
    )
    args = build_docker_run_args(
        dk, "img", ["codex", "run"], "ws-1", {}, needs_plugin_config=False
    )
    ro_plugin_mounts = [a for a in args if "plugins/" in a and ":ro" in a]
    assert ro_plugin_mounts == []


def test_build_args_default_is_plugin_config_false(tmp_path):
    """Omitting the kwarg behaves identically to passing False — hooks must remain safe."""
    host_dir = str(tmp_path / "claude")
    _write_plugin_fixture(host_dir)

    dk = DockerConfig(
        enabled=True,
        default_image="img",
        inherit_claude_config=True,
        host_claude_dir=host_dir,
    )
    args = build_docker_run_args(dk, "img", ["sh", "-c", "echo hi"], "ws-1", {})
    ro_plugin_mounts = [a for a in args if "plugins/" in a and ":ro" in a]
    assert ro_plugin_mounts == []


def test_build_args_with_plugin_config_adds_ro_mounts(tmp_path):
    """Claude Code dispatches (needs_plugin_config=True) add both plugin :ro mounts in non-DooD mode."""
    host_dir = str(tmp_path / "claude")
    _write_plugin_fixture(host_dir)

    dk = DockerConfig(
        enabled=True,
        default_image="img",
        inherit_claude_config=True,
        host_claude_dir=host_dir,
    )
    args = build_docker_run_args(
        dk, "img", ["claude", "-p", "hi"], "ws-1", {}, needs_plugin_config=True
    )
    ro_plugin_mounts = [a for a in args if "plugins/" in a and ":ro" in a]
    assert len(ro_plugin_mounts) == 2
    assert any("installed_plugins.json:ro" in m for m in ro_plugin_mounts)
    assert any("known_marketplaces.json:ro" in m for m in ro_plugin_mounts)


def test_build_args_no_bash_fixup_wrapper(tmp_path):
    """The destructive bash cp-back fallback must not reappear in any code path."""
    host_dir = str(tmp_path / "claude")
    _write_plugin_fixture(host_dir)

    dk = DockerConfig(
        enabled=True,
        default_image="img",
        inherit_claude_config=True,
        host_claude_dir=host_dir,
    )
    args = build_docker_run_args(
        dk, "img", ["claude", "-p", "hi"], "ws-1", {}, needs_plugin_config=True
    )
    # The command should follow the image directly — no bash -c wrapper
    assert "bash" not in args
    assert args[-3:] == ["claude", "-p", "hi"]


# -----------------------------------------------------------------------------
# Unit 2: _prepare_plugin_file raises on DooD shim misconfiguration
# -----------------------------------------------------------------------------


def test_prepare_plugin_file_raises_when_shim_container_dir_missing(tmp_path):
    """DooD dispatch with a non-existent shim_container_dir must raise, not silently soft-degrade."""
    host_dir = str(tmp_path / "claude")
    _write_plugin_fixture(host_dir)
    missing_shim = str(tmp_path / "does-not-exist")

    with pytest.raises(RuntimeError, match="DooD shim directory"):
        _prepare_plugin_file(
            host_dir,
            "/home/agent",
            "plugins/installed_plugins.json",
            read_from_dir=host_dir,
            shim_host_dir="/fake-host-shim",
            shim_container_dir=missing_shim,
        )


def test_prepare_plugin_file_raises_when_shim_points_at_a_file(tmp_path):
    """A shim_container_dir that exists but is a file (not a directory) is still a misconfiguration."""
    host_dir = str(tmp_path / "claude")
    _write_plugin_fixture(host_dir)
    not_a_dir = tmp_path / "whoops"
    not_a_dir.write_text("oops")

    with pytest.raises(RuntimeError, match="DooD shim directory"):
        _prepare_plugin_file(
            host_dir,
            "/home/agent",
            "plugins/installed_plugins.json",
            read_from_dir=host_dir,
            shim_host_dir="/fake-host-shim",
            shim_container_dir=str(not_a_dir),
        )


def test_prepare_plugin_file_returns_none_when_source_absent(tmp_path):
    """Operator with no plugins installed: source file missing should return None (not raise).

    Applies in both DooD and non-DooD mode. Zero-plugin operators must still boot.
    """
    host_dir = str(tmp_path / "claude")  # empty — no plugins subdir
    os.makedirs(host_dir)
    shim_dir = str(tmp_path / "shim")
    os.makedirs(shim_dir)

    # DooD mode, but source file doesn't exist
    result = _prepare_plugin_file(
        host_dir,
        "/home/agent",
        "plugins/installed_plugins.json",
        read_from_dir=host_dir,
        shim_host_dir="/fake-host-shim",
        shim_container_dir=shim_dir,
    )
    assert result is None

    # Non-DooD mode, source file doesn't exist
    result = _prepare_plugin_file(
        host_dir,
        "/home/agent",
        "plugins/installed_plugins.json",
    )
    assert result is None


def test_prepare_plugin_file_succeeds_when_shim_is_valid(tmp_path):
    """Happy path: valid shim writes the rewritten file and returns the host-resolvable path."""
    host_dir = str(tmp_path / "claude")
    _write_plugin_fixture(host_dir)
    shim_container = tmp_path / "shim-container"
    shim_container.mkdir()

    out = _prepare_plugin_file(
        host_dir,
        "/home/agent",
        "plugins/installed_plugins.json",
        read_from_dir=host_dir,
        shim_host_dir="/fake-host-shim",
        shim_container_dir=str(shim_container),
    )
    assert out is not None
    assert out.startswith("/fake-host-shim/")
    # Verify the rewritten file was actually written and has container paths
    written = shim_container / os.path.basename(out)
    assert written.is_file()
    content = json.loads(written.read_text())
    assert "/home/agent/.claude/" in content["installPath"]
    assert host_dir not in content["installPath"]


def test_build_args_raises_on_dood_shim_missing_when_plugin_config_requested(tmp_path):
    """Integration: build_docker_run_args with needs_plugin_config=True propagates the RuntimeError."""
    host_dir = str(tmp_path / "claude")
    _write_plugin_fixture(host_dir)

    dk = DockerConfig(
        enabled=True,
        default_image="img",
        inherit_claude_config=True,
        host_claude_dir=host_dir,
        host_claude_dir_mount=host_dir,
        plugin_shim_host_path="/fake-host-shim",
        plugin_shim_container_path=str(tmp_path / "does-not-exist"),
    )

    with pytest.raises(RuntimeError, match="DooD shim directory"):
        build_docker_run_args(
            dk, "img", ["claude", "-p", "hi"], "ws-1", {}, needs_plugin_config=True
        )


def test_build_args_does_not_raise_when_plugin_config_not_needed(tmp_path):
    """Codex / hooks in DooD mode with broken shim config must still dispatch — they don't use plugin config."""
    host_dir = str(tmp_path / "claude")
    _write_plugin_fixture(host_dir)

    dk = DockerConfig(
        enabled=True,
        default_image="img",
        inherit_claude_config=True,
        host_claude_dir=host_dir,
        host_claude_dir_mount=host_dir,
        plugin_shim_host_path="/fake-host-shim",
        plugin_shim_container_path=str(tmp_path / "does-not-exist"),
    )

    # Should NOT raise — Codex/hook dispatches bypass plugin prep entirely
    args = build_docker_run_args(
        dk, "img", ["codex", "run"], "ws-1", {}, needs_plugin_config=False
    )
    assert "codex" in args


# -----------------------------------------------------------------------------
# Unit 3: startup validation is scoped to Claude Code states
# -----------------------------------------------------------------------------


def _claude_state() -> StateConfig:
    return StateConfig(name="a", type="agent", prompt="p.md", linear_state="active", runner="claude")


def _codex_state() -> StateConfig:
    return StateConfig(name="a", type="agent", prompt="p.md", linear_state="active", runner="codex")


def _terminal_state() -> StateConfig:
    return StateConfig(name="done", type="terminal", linear_state="terminal")


def test_validation_fails_in_dood_with_claude_state_and_missing_shim():
    """DooD + inherit + Claude state + no shim fields → validation error naming the missing fields."""
    cfg = _minimal_service_config(
        {"a": _claude_state(), "done": _terminal_state()},
        DockerConfig(enabled=True, default_image="img", inherit_claude_config=True),
    )
    with _fake_dockerenv(True):
        errs = validate_config(cfg)
    shim_errs = [e for e in errs if "shim" in e.lower()]
    assert len(shim_errs) == 1
    assert "docker.host_claude_dir_mount" in shim_errs[0]
    assert "docker.plugin_shim_host_path" in shim_errs[0]
    assert "docker.plugin_shim_container_path" in shim_errs[0]


def test_validation_error_names_offending_claude_states():
    """Error message should name the Claude Code state(s) that triggered the requirement."""
    cfg = _minimal_service_config(
        {"a": _claude_state(), "done": _terminal_state()},
        DockerConfig(enabled=True, default_image="img", inherit_claude_config=True),
    )
    with _fake_dockerenv(True):
        errs = validate_config(cfg)
    shim_errs = [e for e in errs if "shim" in e.lower()]
    assert len(shim_errs) == 1
    assert "'a'" in shim_errs[0] or "a" in shim_errs[0]


def test_validation_passes_in_dood_with_only_codex_states():
    """DooD + inherit + Codex-only states + no shim fields → no shim error (R4)."""
    cfg = _minimal_service_config(
        {"a": _codex_state(), "done": _terminal_state()},
        DockerConfig(enabled=True, default_image="img", inherit_claude_config=True),
    )
    with _fake_dockerenv(True):
        errs = validate_config(cfg)
    shim_errs = [e for e in errs if "shim" in e.lower()]
    assert shim_errs == []


def test_validation_passes_when_shim_fields_set():
    """DooD + inherit + Claude state + all three shim fields set → no shim error."""
    cfg = _minimal_service_config(
        {"a": _claude_state(), "done": _terminal_state()},
        DockerConfig(
            enabled=True,
            default_image="img",
            inherit_claude_config=True,
            host_claude_dir_mount="/host-claude",
            plugin_shim_host_path="/var/shim",
            plugin_shim_container_path="/shim",
        ),
    )
    with _fake_dockerenv(True):
        errs = validate_config(cfg)
    shim_errs = [e for e in errs if "shim" in e.lower()]
    assert shim_errs == []


def test_validation_passes_outside_dood():
    """Non-DooD (/.dockerenv absent) + Claude state + no shim fields → no shim error."""
    cfg = _minimal_service_config(
        {"a": _claude_state(), "done": _terminal_state()},
        DockerConfig(enabled=True, default_image="img", inherit_claude_config=True),
    )
    with _fake_dockerenv(False):
        errs = validate_config(cfg)
    shim_errs = [e for e in errs if "shim" in e.lower()]
    assert shim_errs == []


def test_validation_passes_when_inherit_disabled():
    """DooD + inherit_claude_config=false + Claude state + no shim fields → no shim error."""
    cfg = _minimal_service_config(
        {"a": _claude_state(), "done": _terminal_state()},
        DockerConfig(enabled=True, default_image="img", inherit_claude_config=False),
    )
    with _fake_dockerenv(True):
        errs = validate_config(cfg)
    shim_errs = [e for e in errs if "shim" in e.lower()]
    assert shim_errs == []


def test_validation_fires_for_mixed_runners():
    """When Claude and Codex states coexist in DooD, the Claude state still triggers the requirement."""
    cfg = _minimal_service_config(
        {
            "claude_state": StateConfig(
                name="claude_state", type="agent", prompt="p.md",
                linear_state="active", runner="claude",
            ),
            "codex_state": StateConfig(
                name="codex_state", type="agent", prompt="p.md",
                linear_state="active", runner="codex",
            ),
            "done": _terminal_state(),
        },
        DockerConfig(enabled=True, default_image="img", inherit_claude_config=True),
    )
    with _fake_dockerenv(True):
        errs = validate_config(cfg)
    shim_errs = [e for e in errs if "shim" in e.lower()]
    assert len(shim_errs) == 1
    assert "claude_state" in shim_errs[0]
    assert "codex_state" not in shim_errs[0]
