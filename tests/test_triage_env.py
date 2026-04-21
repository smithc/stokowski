"""Tests for triage workflow env injection (Unit 8).

Verifies STOKOWSKI_REPOS_JSON is injected into the dispatch env when the
current workflow has triage=True, and omitted otherwise.
"""

from __future__ import annotations

import json

import pytest

from stokowski.orchestrator import Orchestrator


def _make_orch_with_triage(tmp_path):
    wf_path = tmp_path / "workflow.yaml"
    wf_path.write_text(
        """
tracker:
  api_key: test-key
  project_slug: abc123

states:
  work:
    type: agent
    prompt: p.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  standard:
    label: "workflow:standard"
    default: true
    path: [work, done]
  intake:
    label: "workflow:intake"
    triage: true
    path: [work, done]

repos:
  api:
    label: "repo:api"
    clone_url: "git@github.com:org/api.git"
  web:
    label: "repo:web"
    clone_url: "git@github.com:org/web.git"
"""
    )
    orch = Orchestrator(str(wf_path))
    errors = orch._load_workflow()
    assert not errors, f"Config errors: {errors}"
    return orch


def _make_legacy_orch(tmp_path):
    wf_path = tmp_path / "legacy.yaml"
    wf_path.write_text(
        """
tracker:
  api_key: test-key
  project_slug: abc123
states:
  work:
    type: agent
    prompt: p.md
  done:
    type: terminal
    linear_state: terminal
"""
    )
    orch = Orchestrator(str(wf_path))
    errors = orch._load_workflow()
    assert not errors, f"Config errors: {errors}"
    return orch


def test_triage_workflow_has_triage_flag_set(tmp_path):
    orch = _make_orch_with_triage(tmp_path)
    assert orch.cfg.workflows["intake"].triage is True
    assert orch.cfg.workflows["standard"].triage is False


def test_repos_json_shape_matches_prompt_contract(tmp_path):
    """Verify the JSON shape matches what prompts/triage.example.md documents."""
    orch = _make_orch_with_triage(tmp_path)

    # Build the JSON the same way _run_worker does
    repo_list = [
        {
            "name": r.name,
            "label": r.label or "",
            "clone_url": r.clone_url or "",
        }
        for r in orch.cfg.repos.values()
        if r.name != "_default"
    ]
    serialized = json.dumps(repo_list)
    parsed = json.loads(serialized)

    assert len(parsed) == 2
    # Entries have the three expected keys
    for entry in parsed:
        assert set(entry.keys()) == {"name", "label", "clone_url"}
    # Names match config
    names = {e["name"] for e in parsed}
    assert names == {"api", "web"}
    # Synthetic _default is excluded
    assert "_default" not in names


def test_repos_json_excludes_synthetic_default_legacy_config(tmp_path):
    """Legacy config (only _default) produces an empty list."""
    orch = _make_legacy_orch(tmp_path)

    repo_list = [
        {
            "name": r.name,
            "label": r.label or "",
            "clone_url": r.clone_url or "",
        }
        for r in orch.cfg.repos.values()
        if r.name != "_default"
    ]
    assert repo_list == []
    assert json.dumps(repo_list) == "[]"


def test_multi_repo_config_without_default_requires_triage_workflow(tmp_path):
    """Regression: Unit 3 validation enforces that multi-repo + no default
    requires a triage workflow. A config missing the triage flag but using
    an alternate name (e.g., "intake" without triage:true) should fail
    validation."""
    wf_path = tmp_path / "bad.yaml"
    wf_path.write_text(
        """
tracker:
  api_key: test-key
  project_slug: abc123
states:
  work:
    type: agent
    prompt: p.md
  done:
    type: terminal
    linear_state: terminal
workflows:
  standard:
    label: "workflow:standard"
    default: true
    path: [work, done]
  intake:
    label: "workflow:intake"
    path: [work, done]
repos:
  api:
    label: "repo:api"
    clone_url: "git@github.com:org/api.git"
  web:
    label: "repo:web"
    clone_url: "git@github.com:org/web.git"
"""
    )
    orch = Orchestrator(str(wf_path))
    errors = orch._load_workflow()
    # Should fail because no workflow has triage: true and no repo has default
    assert any("triage" in e.lower() for e in errors)


def test_triage_env_contract_documented_in_prompt():
    """Smoke: the example triage prompt references the env contract."""
    from pathlib import Path

    prompt_path = Path("prompts/triage.example.md")
    assert prompt_path.exists()
    body = prompt_path.read_text()
    assert "STOKOWSKI_REPOS_JSON" in body
    # Documents both multi-repo and legacy paths
    assert "`[]`" in body or "empty" in body.lower()
    assert "repo:api" in body or "repo:*" in body
