"""Tests for workspace key composition, ensure/remove signatures, and
persistent-mode scheduled-job workspaces.

Focuses on:
- Unit 4: composite `{issue}-{repo}` key shape
- Unit 7 (scheduled-jobs): ``workspace_key`` override and ``skip_removal``
  flag that enable persistent-mode scheduled-job workspaces

Docker-path tests live in tests/test_docker_runner.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from stokowski.config import HooksConfig
from stokowski.workspace import (
    WorkspaceResult,
    compose_workspace_key,
    ensure_workspace,
    remove_workspace,
    sanitize_key,
)


def _run(coro):
    """Run an async coroutine in a fresh event loop. Keeps tests sync-shaped
    to match the existing test_cancel.py / test_docker_runner.py pattern."""
    return asyncio.run(coro)


# ── compose_workspace_key ───────────────────────────────────────────────────


def test_compose_workspace_key_basic():
    # `{len}-{issue}-{repo}` — length prefix makes parsing unambiguous
    assert compose_workspace_key("SMI-14", "api") == "6-SMI-14-api"


def test_compose_workspace_key_legacy_default():
    """Legacy synthesized _default repo carries the length prefix too."""
    assert compose_workspace_key("SMI-14", "_default") == "6-SMI-14-_default"


def test_compose_workspace_key_sanitizes_repo():
    """Slashes in repo name are sanitized (path safety)."""
    assert compose_workspace_key("SMI-14", "my/repo") == "6-SMI-14-my_repo"


def test_compose_workspace_key_sanitizes_issue():
    """Slashes in issue identifier are also sanitized."""
    assert compose_workspace_key("SMI/14", "api") == "6-SMI_14-api"


def test_compose_workspace_key_preserves_safe_chars():
    """Underscores, dots, hyphens all pass through."""
    assert compose_workspace_key("SMI-14", "py.service_3") == "6-SMI-14-py.service_3"


def test_compose_workspace_key_distinct_per_repo():
    """Two repos on the same issue produce distinct keys."""
    key_a = compose_workspace_key("SMI-14", "api")
    key_b = compose_workspace_key("SMI-14", "web")
    assert key_a != key_b


def test_compose_workspace_key_distinct_per_issue():
    """Two issues on the same repo produce distinct keys."""
    key_a = compose_workspace_key("SMI-14", "api")
    key_b = compose_workspace_key("SMI-15", "api")
    assert key_a != key_b


def test_compose_workspace_key_adv_001_collision_prevented():
    """ADV-001 regression: before the length prefix, `(SMI-my, repo)` and
    `(SMI, my-repo)` both mapped to `SMI-my-repo` and shared a workspace
    directory. The length prefix distinguishes them."""
    key_a = compose_workspace_key("SMI-my", "repo")   # "6-SMI-my-repo"
    key_b = compose_workspace_key("SMI", "my-repo")    # "3-SMI-my-repo"
    assert key_a != key_b


def test_compose_workspace_key_hyphen_heavy_components_distinct():
    """Exhaustive cross-component hyphen check — every 3-split of `a-b-c`
    over (issue, repo) must produce a distinct key."""
    keys = {
        compose_workspace_key("a-b-c", "x"),
        compose_workspace_key("a-b", "c-x"),
        compose_workspace_key("a", "b-c-x"),
        compose_workspace_key("a-b-c-x", ""),  # empty repo sanitizes to "" → "5-a-b-c-x-"
    }
    # The fourth one with empty repo is a bit degenerate but still should
    # produce a distinct value from the other three
    # Every key must be unique (no collisions)
    assert len(keys) == 4


# ── ensure_workspace / remove_workspace signatures (non-Docker path) ────────


def test_ensure_workspace_creates_composite_path(tmp_path):
    """ensure_workspace creates `{root}/{len}-{issue}-{repo}`."""
    hooks = HooksConfig()  # no hooks, just directory creation
    result = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))

    expected = tmp_path / "6-SMI-14-api"
    assert expected.exists()
    assert expected.is_dir()
    assert result.path == expected
    assert result.workspace_key == "6-SMI-14-api"
    assert result.created_now is True


def test_ensure_workspace_reuses_existing_dir(tmp_path):
    """Second call to ensure_workspace for same (issue, repo) reuses."""
    hooks = HooksConfig()
    first = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))
    second = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))

    assert first.path == second.path
    assert second.created_now is False


def test_ensure_workspace_different_repos_different_paths(tmp_path):
    """Same issue, different repos → different workspace dirs."""
    hooks = HooksConfig()
    ws_a = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))
    ws_b = _run(ensure_workspace(tmp_path, "SMI-14", "web", hooks))

    assert ws_a.path != ws_b.path
    assert ws_a.path.exists()
    assert ws_b.path.exists()


def test_ensure_workspace_legacy_default_repo(tmp_path):
    """Legacy `_default` repo produces the length-prefixed key."""
    hooks = HooksConfig()
    result = _run(ensure_workspace(tmp_path, "SMI-14", "_default", hooks))

    assert result.path == tmp_path / "6-SMI-14-_default"
    assert result.workspace_key == "6-SMI-14-_default"


def test_remove_workspace_removes_composite_path(tmp_path):
    """remove_workspace removes the composite-keyed dir."""
    hooks = HooksConfig()
    ws = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))
    assert ws.path.exists()

    _run(remove_workspace(tmp_path, "SMI-14", "api", hooks))
    assert not ws.path.exists()


def test_remove_workspace_missing_is_idempotent(tmp_path):
    """remove_workspace on non-existent dir is a silent no-op."""
    hooks = HooksConfig()
    # Should not raise
    _run(remove_workspace(tmp_path, "SMI-14", "api", hooks))


def test_remove_workspace_only_targets_specified_repo(tmp_path):
    """Removing (issue, api) leaves (issue, web) workspace intact."""
    hooks = HooksConfig()
    ws_a = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))
    ws_b = _run(ensure_workspace(tmp_path, "SMI-14", "web", hooks))

    _run(remove_workspace(tmp_path, "SMI-14", "api", hooks))

    assert not ws_a.path.exists()
    assert ws_b.path.exists()


# ---------------------------------------------------------------------------
# ensure_workspace: workspace_key override (Unit 7, persistent scheduled jobs)
# ---------------------------------------------------------------------------


class TestEnsureWorkspaceKey:
    def test_default_behavior_uses_composite_key(self, tmp_path: Path):
        """Without workspace_key override, falls through to the composite
        `{len}-{issue}-{repo}` key produced by compose_workspace_key."""
        hooks = HooksConfig()
        result = asyncio.run(
            ensure_workspace(tmp_path, "ISSUE-42", "_default", hooks)
        )
        expected_key = compose_workspace_key("ISSUE-42", "_default")
        assert result.workspace_key == expected_key
        assert result.path == tmp_path / expected_key
        assert result.created_now is True
        assert result.path.exists()

    def test_workspace_key_overrides_composite(self, tmp_path: Path):
        hooks = HooksConfig()
        result = asyncio.run(
            ensure_workspace(
                tmp_path, "CHILD-101", "_default", hooks,
                workspace_key="TMPL-001",
            )
        )
        assert result.workspace_key == "TMPL-001"
        assert result.path == tmp_path / "TMPL-001"
        assert result.created_now is True

    def test_workspace_key_sanitized_defensively(self, tmp_path: Path):
        # Caller passes a raw string with path-traversal chars; sanitize_key
        # neutralizes them so the safety check at ensure_workspace passes.
        hooks = HooksConfig()
        result = asyncio.run(
            ensure_workspace(
                tmp_path, "ignored", "_default", hooks,
                workspace_key="../escape",
            )
        )
        # sanitize_key replaces '/' -> '_', leaving ".._escape"
        assert result.workspace_key == sanitize_key("../escape")
        # Path must remain under tmp_path (safety invariant)
        assert result.path.resolve().is_relative_to(tmp_path.resolve())

    def test_second_call_same_key_reuses_workspace(self, tmp_path: Path):
        # Simulates persistent-mode: first fire creates, subsequent fires
        # detect created_now=False and skip after_create.
        hooks = HooksConfig()
        first = asyncio.run(
            ensure_workspace(
                tmp_path, "CHILD-1", "_default", hooks,
                workspace_key="TMPL-1",
            )
        )
        # Drop a marker file that should survive
        marker = first.path / ".marker"
        marker.write_text("survive-across-fires")

        second = asyncio.run(
            ensure_workspace(
                tmp_path, "CHILD-2", "_default", hooks,
                workspace_key="TMPL-1",
            )
        )
        assert second.workspace_key == "TMPL-1"
        assert second.path == first.path
        assert second.created_now is False
        assert marker.exists()
        assert marker.read_text() == "survive-across-fires"

    def test_ephemeral_creates_per_child(self, tmp_path: Path):
        # Regression: without workspace_key, each identifier gets its own dir.
        hooks = HooksConfig()
        a = asyncio.run(ensure_workspace(tmp_path, "A-1", "_default", hooks))
        b = asyncio.run(ensure_workspace(tmp_path, "A-2", "_default", hooks))
        assert a.path != b.path
        assert a.created_now is True
        assert b.created_now is True


# ---------------------------------------------------------------------------
# remove_workspace: skip_removal flag (Unit 7)
# ---------------------------------------------------------------------------


class TestRemoveWorkspaceSkipRemoval:
    def test_default_removal_destroys_directory(self, tmp_path: Path):
        hooks = HooksConfig()
        ws_path = tmp_path / compose_workspace_key("ISSUE-1", "_default")
        ws_path.mkdir()
        (ws_path / "file.txt").write_text("data")

        asyncio.run(remove_workspace(tmp_path, "ISSUE-1", "_default", hooks))
        assert not ws_path.exists()

    def test_skip_removal_preserves_directory(self, tmp_path: Path):
        hooks = HooksConfig()
        ws_path = tmp_path / "TMPL-1"
        ws_path.mkdir()
        (ws_path / "file.txt").write_text("persistent")

        asyncio.run(
            remove_workspace(
                tmp_path, "CHILD-1", "_default", hooks,
                workspace_key="TMPL-1",
                skip_removal=True,
            )
        )
        assert ws_path.exists()
        assert (ws_path / "file.txt").read_text() == "persistent"

    def test_skip_removal_runs_before_remove_hook(self, tmp_path: Path):
        # Hook writes a marker — proves before_remove executed even with
        # skip_removal=True.
        marker = tmp_path / "TMPL-1" / "hook-ran"
        ws_path = tmp_path / "TMPL-1"
        ws_path.mkdir()

        hooks = HooksConfig(
            before_remove=f"touch {marker}",
            timeout_ms=5000,
        )
        asyncio.run(
            remove_workspace(
                tmp_path, "CHILD-1", "_default", hooks,
                workspace_key="TMPL-1",
                skip_removal=True,
            )
        )
        assert ws_path.exists(), "dir must survive skip_removal=True"
        assert marker.exists(), "before_remove hook must still run"

    def test_skip_removal_false_runs_hook_and_destroys(self, tmp_path: Path):
        ws_path = tmp_path / compose_workspace_key("EPH-1", "_default")
        ws_path.mkdir()
        marker_src = ws_path / "evidence"

        hooks = HooksConfig(
            before_remove=f"touch {marker_src}",
            timeout_ms=5000,
        )
        asyncio.run(
            remove_workspace(
                tmp_path, "EPH-1", "_default", hooks, skip_removal=False
            )
        )
        # Directory destroyed; we can only verify by absence.
        assert not ws_path.exists()

    def test_workspace_key_routes_through_sanitize_key(self, tmp_path: Path):
        # Create dir under sanitized key, then request removal with raw key.
        sanitized = sanitize_key("../escape")
        (tmp_path / sanitized).mkdir()

        hooks = HooksConfig()
        asyncio.run(
            remove_workspace(
                tmp_path, "unused", "_default", hooks,
                workspace_key="../escape",
            )
        )
        assert not (tmp_path / sanitized).exists()


# ---------------------------------------------------------------------------
# Orchestrator routing helper: _remove_workspace_for_child
# ---------------------------------------------------------------------------


class TestRemoveWorkspaceForChild:
    """Unit tests for Orchestrator._remove_workspace_for_child routing.

    Uses a minimal stub orchestrator rather than real construction. The
    helper touches: self._child_to_template, self._template_snapshots,
    self._resolve_schedule_config_for_template, self._cfg_for_issue_or_primary,
    self._get_issue_repo_config, and the resolved cfg's workspace/docker/hooks.
    """

    def _make_stub(self, tmp_path: Path, *, schedule_cfg=None, template=None):
        from types import SimpleNamespace

        from stokowski.orchestrator import Orchestrator
        from stokowski.config import RepoConfig

        default_repo = RepoConfig(
            name="_default",
            label=None,
            clone_url="",
            default=True,
            docker_image=None,
        )
        cfg_stub = SimpleNamespace(
            workspace=SimpleNamespace(resolved_root=lambda: tmp_path),
            docker=SimpleNamespace(enabled=False),
            hooks=HooksConfig(),
            repos={"_default": default_repo},
            repos_synthesized=True,
        )
        workflow_stub = SimpleNamespace(config=cfg_stub)

        stub = Orchestrator.__new__(Orchestrator)
        # Multi-project wiring: make _cfg_for_issue_or_primary / _primary_cfg
        # both return our cfg stub.
        stub.configs = {"_primary": workflow_stub}
        stub._issue_project = {}
        stub._issue_repo = {}
        stub._last_issues = {}
        stub._child_to_template = {}
        stub._template_snapshots = {}
        stub._templates = set()
        if template is not None:
            stub._templates.add(template.id)
            stub._template_snapshots[template.id] = template
            stub._child_to_template["child-id"] = template.id
        # Stub _resolve_schedule_config_for_template (bound method on instance)
        stub._resolve_schedule_config_for_template = lambda _t: schedule_cfg
        return stub

    def test_ephemeral_fallback_destroys_directory(self, tmp_path: Path):
        from stokowski.orchestrator import Orchestrator

        stub = self._make_stub(tmp_path)
        ws_path = tmp_path / compose_workspace_key("ISSUE-99", "_default")
        ws_path.mkdir()

        asyncio.run(
            Orchestrator._remove_workspace_for_child(
                stub, "some-id", "ISSUE-99"
            )
        )
        assert not ws_path.exists()

    def test_persistent_with_template_preserves_workspace(self, tmp_path: Path):
        from stokowski.config import ScheduleConfig
        from stokowski.orchestrator import Orchestrator
        from stokowski.models import Issue

        template = Issue(
            id="tmpl-1",
            identifier="TMPL-1",
            title="t",
            state="Scheduled",
            labels=["schedule:daily"],
        )
        sched = ScheduleConfig(name="daily", workspace_mode="persistent")
        stub = self._make_stub(tmp_path, schedule_cfg=sched, template=template)

        # Template's workspace exists (created on first fire) at the key
        # ensure_workspace uses when workspace_key="TMPL-1" is passed.
        tmpl_ws = tmp_path / sanitize_key("TMPL-1")
        tmpl_ws.mkdir()
        (tmpl_ws / "data.txt").write_text("keep me")

        asyncio.run(
            Orchestrator._remove_workspace_for_child(
                stub, "child-id", "CHILD-100"
            )
        )
        assert tmpl_ws.exists(), "persistent workspace must survive child terminal"
        assert (tmpl_ws / "data.txt").read_text() == "keep me"
        # Child-specific dir should never have been touched (never existed)
        assert not (tmp_path / compose_workspace_key("CHILD-100", "_default")).exists()

    def test_ephemeral_scheduled_child_still_destroys(self, tmp_path: Path):
        from stokowski.config import ScheduleConfig
        from stokowski.orchestrator import Orchestrator
        from stokowski.models import Issue

        template = Issue(
            id="tmpl-2",
            identifier="TMPL-2",
            title="t",
            state="Scheduled",
            labels=["schedule:hourly"],
        )
        sched = ScheduleConfig(name="hourly", workspace_mode="ephemeral")
        stub = self._make_stub(tmp_path, schedule_cfg=sched, template=template)

        child_ws = tmp_path / compose_workspace_key("CHILD-200", "_default")
        child_ws.mkdir()

        asyncio.run(
            Orchestrator._remove_workspace_for_child(
                stub, "child-id", "CHILD-200"
            )
        )
        assert not child_ws.exists(), "ephemeral child workspace should be destroyed"
