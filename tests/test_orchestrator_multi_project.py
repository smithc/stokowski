"""Tests for multi-project orchestrator state model (Unit 2, 3)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from stokowski.orchestrator import Orchestrator


_PROJECT_YAML = """\
tracker:
  project_slug: {slug}
  api_key: dummy_{slug}
polling:
  interval_ms: {poll}
workspace:
  root: /tmp/ws-{slug}
states:
  work:
    type: agent
    prompt: p.md
  done:
    type: terminal
    linear_state: terminal
"""


def _write(tmp_path: Path, slug: str, poll: int = 15000) -> Path:
    path = tmp_path / f"workflow.{slug}.yaml"
    path.write_text(_PROJECT_YAML.format(slug=slug, poll=poll))
    return path


# ── Constructor shapes ──────────────────────────────────────────────────────


def test_single_path_legacy_constructor(tmp_path):
    p = _write(tmp_path, "alpha")
    orch = Orchestrator(p)
    assert orch.workflow_paths == [p]
    assert orch.workflow_path == p  # back-compat attribute


def test_single_path_as_string(tmp_path):
    p = _write(tmp_path, "alpha")
    orch = Orchestrator(str(p))
    assert orch.workflow_paths == [p]


def test_list_of_paths_constructor(tmp_path):
    a = _write(tmp_path, "alpha")
    b = _write(tmp_path, "beta")
    orch = Orchestrator([a, b])
    assert orch.workflow_paths == [a, b]
    assert orch.workflow_path == a  # primary = first


def test_empty_list_raises(tmp_path):
    with pytest.raises(ValueError):
        Orchestrator([])


# ── Helper methods ──────────────────────────────────────────────────────────


def test_cfg_for_issue_unknown_raises(tmp_path):
    p = _write(tmp_path, "alpha")
    orch = Orchestrator(p)
    orch._load_all_workflows()
    with pytest.raises(RuntimeError, match="No project binding"):
        orch._cfg_for_issue("unknown-id")


def test_cfg_for_issue_returns_right_project(tmp_path):
    a = _write(tmp_path, "alpha")
    b = _write(tmp_path, "beta")
    orch = Orchestrator([a, b])
    orch._load_all_workflows()

    orch._issue_project["iid-a"] = "alpha"
    orch._issue_project["iid-b"] = "beta"

    cfg_a = orch._cfg_for_issue("iid-a")
    cfg_b = orch._cfg_for_issue("iid-b")
    assert cfg_a.tracker.project_slug == "alpha"
    assert cfg_b.tracker.project_slug == "beta"


def test_primary_cfg_returns_first_loaded(tmp_path):
    a = _write(tmp_path, "alpha")
    b = _write(tmp_path, "beta")
    orch = Orchestrator([a, b])
    orch._load_all_workflows()
    assert orch._primary_cfg().tracker.project_slug == "alpha"


def test_primary_cfg_before_load_raises(tmp_path):
    p = _write(tmp_path, "alpha")
    orch = Orchestrator(p)
    with pytest.raises(RuntimeError, match="_load_all_workflows"):
        orch._primary_cfg()


def test_linear_client_for_constructs_lazily(tmp_path):
    a = _write(tmp_path, "alpha")
    b = _write(tmp_path, "beta")
    orch = Orchestrator([a, b])
    orch._load_all_workflows()

    assert "alpha" not in orch._linear_clients
    client_a = orch._linear_client_for("alpha")
    assert "alpha" in orch._linear_clients
    # Cached
    assert orch._linear_client_for("alpha") is client_a
    # Distinct per project
    client_b = orch._linear_client_for("beta")
    assert client_a is not client_b


def test_linear_client_for_unknown_raises(tmp_path):
    p = _write(tmp_path, "alpha")
    orch = Orchestrator(p)
    orch._load_all_workflows()
    with pytest.raises(RuntimeError, match="No config loaded"):
        orch._linear_client_for("nonexistent")


def test_workflow_dir_for_issue(tmp_path):
    a = _write(tmp_path, "alpha")
    b_dir = tmp_path / "sub"
    b_dir.mkdir()
    b = b_dir / "workflow.beta.yaml"
    b.write_text(_PROJECT_YAML.format(slug="beta", poll=15000))
    orch = Orchestrator([a, b])
    orch._load_all_workflows()

    orch._issue_project["iid-a"] = "alpha"
    orch._issue_project["iid-b"] = "beta"

    assert orch._workflow_dir_for_issue("iid-a") == tmp_path
    assert orch._workflow_dir_for_issue("iid-b") == b_dir


# ── Cleanup parity meta-test ────────────────────────────────────────────────


def test_cleanup_issue_state_includes_issue_project(tmp_path):
    """Regression: _cleanup_issue_state must remove _issue_project entries."""
    p = _write(tmp_path, "alpha")
    orch = Orchestrator(p)
    orch._load_all_workflows()

    orch._issue_project["iid-x"] = "alpha"
    orch._cleanup_issue_state("iid-x")
    assert "iid-x" not in orch._issue_project


def test_cleanup_issue_state_parity_covers_all_per_issue_dicts(tmp_path):
    """Meta-test: every per-issue state dict/set is touched by cleanup.

    Walks __dict__ for dict/set attributes that look per-issue-keyed.
    A missed cleanup would leak state after terminal transitions.
    """
    p = _write(tmp_path, "alpha")
    orch = Orchestrator(p)
    orch._load_all_workflows()

    # Known per-issue state attributes — names ending in these suffixes or
    # known explicitly. Updated whenever we add a new per-issue cache.
    known_per_issue = {
        "running", "claimed", "retry_attempts", "completed",
        "_tasks", "_retry_timers", "_last_session_ids", "_last_issues",
        "_last_completed_at", "_issue_current_state", "_issue_state_runs",
        "_pending_gates", "_issue_workflow", "_issue_repo", "_issue_project",
        "_rejected_issues", "_migrated_issues", "_config_blocked",
        "_rejection_fetch_pending", "_prev_issue_labels", "_force_cancelled",
    }

    for name in known_per_issue:
        assert hasattr(orch, name), f"Expected per-issue state attr {name!r}"

    issue_id = "iid-meta"
    # Stamp something into each
    orch.running[issue_id] = object()  # type: ignore[assignment]
    orch.claimed.add(issue_id)
    orch._tasks[issue_id] = object()  # type: ignore[assignment]
    orch._last_session_ids[issue_id] = "s"
    orch._last_completed_at[issue_id] = object()  # type: ignore[assignment]
    orch._issue_current_state[issue_id] = "work"
    orch._issue_state_runs[issue_id] = 1
    orch._pending_gates[issue_id] = "gate"
    orch._issue_workflow[issue_id] = "default"
    orch._issue_repo[issue_id] = "api"
    orch._issue_project[issue_id] = "alpha"
    orch._rejected_issues.add(issue_id)
    orch._migrated_issues.add(issue_id)
    orch._config_blocked.add(issue_id)
    orch._rejection_fetch_pending.add(issue_id)
    orch._prev_issue_labels[issue_id] = []
    orch._last_issues[issue_id] = object()  # type: ignore[assignment]

    orch._cleanup_issue_state(issue_id)

    # After cleanup, none of the per-issue state should reference the id.
    per_issue_keyed = [
        orch.running, orch.claimed, orch._tasks, orch._last_session_ids,
        orch._last_completed_at, orch._issue_current_state,
        orch._issue_state_runs, orch._pending_gates, orch._issue_workflow,
        orch._issue_repo, orch._issue_project, orch._rejected_issues,
        orch._migrated_issues, orch._config_blocked,
        orch._rejection_fetch_pending, orch._prev_issue_labels,
        orch._last_issues,
    ]
    for coll in per_issue_keyed:
        if isinstance(coll, dict):
            assert issue_id not in coll, f"{type(coll).__name__} still has {issue_id}"
        else:
            assert issue_id not in coll, f"set still has {issue_id}"


# ── Loading ────────────────────────────────────────────────────────────────


def test_load_all_workflows_populates_configs(tmp_path):
    a = _write(tmp_path, "alpha")
    b = _write(tmp_path, "beta")
    orch = Orchestrator([a, b])
    errors = orch._load_all_workflows()
    assert errors == {}
    assert set(orch.configs.keys()) == {"alpha", "beta"}


def test_load_all_workflows_min_polling_interval(tmp_path):
    a = _write(tmp_path, "alpha", poll=30000)
    b = _write(tmp_path, "beta", poll=5000)
    orch = Orchestrator([a, b])
    orch._load_all_workflows()
    assert orch._polling_interval_ms == 5000


def test_load_all_workflows_duplicate_slug_is_error(tmp_path):
    a = tmp_path / "workflow.one.yaml"
    b = tmp_path / "workflow.two.yaml"
    a.write_text(_PROJECT_YAML.format(slug="shared", poll=15000))
    b.write_text(_PROJECT_YAML.format(slug="shared", poll=15000))
    orch = Orchestrator([a, b])
    errors = orch._load_all_workflows()
    # First one loads clean; second errors on duplicate.
    assert "shared" in errors
    assert any("duplicate" in e for e in errors["shared"])


def test_load_all_workflows_preserves_last_known_good_on_reload_failure(tmp_path):
    a = _write(tmp_path, "alpha")
    orch = Orchestrator([a])
    errors1 = orch._load_all_workflows()
    assert errors1 == {}
    cfg_before = orch.configs["alpha"]

    # Corrupt the file.
    a.write_text("::not valid yaml ::")
    errors2 = orch._load_all_workflows()
    assert errors2  # error surfaced
    # Last-known-good preserved:
    assert orch.configs.get("alpha") is cfg_before
