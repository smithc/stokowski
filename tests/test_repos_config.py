"""Tests for multi-repo config parsing, synthesis, and validation.

Follows the pattern in tests/test_state_machine.py — pure-function tests on
the config module, no mocks, no network, no Linear/Docker. Sibling of the
existing state-machine/workflow tests; kept separate for clarity while
multi-repo plumbing is landing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from stokowski.config import (
    RepoConfig,
    ServiceConfig,
    WorkflowConfig,
    parse_workflow_file,
)


def _write_yaml(content: str) -> Path:
    """Write a YAML fragment to a temp file and return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return Path(f.name)


_MINIMAL_STATES = """
tracker:
  project_slug: abc
  api_key: dummy
states:
  work:
    type: agent
    prompt: p.md
  done:
    type: terminal
    linear_state: terminal
"""


# ── RepoConfig dataclass ────────────────────────────────────────────────────


def test_repo_config_defaults():
    """RepoConfig constructs with sensible defaults."""
    r = RepoConfig(name="api")
    assert r.name == "api"
    assert r.label is None
    assert r.clone_url == ""
    assert r.default is False
    assert r.docker_image is None


def test_repo_config_full():
    """RepoConfig accepts all expected fields."""
    r = RepoConfig(
        name="api",
        label="repo:api",
        clone_url="git@github.com:org/api.git",
        default=True,
        docker_image="stokowski/node:latest",
    )
    assert r.label == "repo:api"
    assert r.clone_url == "git@github.com:org/api.git"
    assert r.default is True
    assert r.docker_image == "stokowski/node:latest"


# ── WorkflowConfig.triage field ─────────────────────────────────────────────


def test_workflow_config_triage_defaults_false():
    """WorkflowConfig.triage defaults to False so existing configs unchanged."""
    w = WorkflowConfig(name="standard")
    assert w.triage is False


def test_workflow_config_triage_explicit():
    """Operators can set triage=True on a workflow."""
    w = WorkflowConfig(name="intake", triage=True)
    assert w.triage is True


# ── Legacy synthesis (no `repos:` section) ──────────────────────────────────


def test_parse_legacy_synthesizes_default_repo():
    """Configs with no `repos:` section get a synthetic _default entry."""
    path = _write_yaml(
        _MINIMAL_STATES
        + """
hooks:
  after_create: 'git clone foo .'
"""
    )
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    assert cfg.repos_synthesized is True
    assert "_default" in cfg.repos
    assert len(cfg.repos) == 1

    default_repo = cfg.repos["_default"]
    assert default_repo.name == "_default"
    assert default_repo.label is None
    assert default_repo.clone_url == ""
    assert default_repo.default is True
    assert default_repo.docker_image is None


def test_parse_legacy_no_hooks_still_synthesizes():
    """Even without hooks, absent `repos:` triggers synthesis."""
    path = _write_yaml(_MINIMAL_STATES)
    parsed = parse_workflow_file(path)

    assert parsed.config.repos_synthesized is True
    assert "_default" in parsed.config.repos


# ── Explicit `repos:` registry ──────────────────────────────────────────────


def test_parse_explicit_repos_registry():
    """Explicit `repos:` section parses each entry correctly."""
    path = _write_yaml(
        _MINIMAL_STATES
        + """
repos:
  api:
    label: repo:api
    clone_url: git@github.com:org/api.git
    default: true
  web:
    label: repo:web
    clone_url: git@github.com:org/web.git
    docker_image: stokowski/node:latest
"""
    )
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    assert cfg.repos_synthesized is False
    assert len(cfg.repos) == 2

    api = cfg.repos["api"]
    assert api.name == "api"
    assert api.label == "repo:api"
    assert api.clone_url == "git@github.com:org/api.git"
    assert api.default is True
    assert api.docker_image is None

    web = cfg.repos["web"]
    assert web.default is False
    assert web.docker_image == "stokowski/node:latest"


def test_parse_explicit_empty_repos_no_synthesis():
    """Explicit `repos: {}` is distinct from absent — NO synthesis.

    Validation (Unit 3) surfaces this as an error; parsing leaves `repos` empty
    so the validator has an unambiguous signal.
    """
    path = _write_yaml(_MINIMAL_STATES + "\nrepos: {}\n")
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    assert cfg.repos_synthesized is False
    assert cfg.repos == {}


# ── WorkflowConfig.triage parsing ───────────────────────────────────────────


def test_parse_workflow_triage_flag():
    """Operators designate a triage workflow via `triage: true`."""
    path = _write_yaml(
        _MINIMAL_STATES
        + """
workflows:
  standard:
    label: workflow:standard
    default: true
    path: [work, done]
  intake:
    label: workflow:intake
    triage: true
    path: [work, done]
"""
    )
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    assert cfg.workflows["standard"].triage is False
    assert cfg.workflows["intake"].triage is True


def test_parse_workflow_triage_defaults_false_when_absent():
    """Workflows without explicit `triage:` default to False (backward compat)."""
    path = _write_yaml(
        _MINIMAL_STATES
        + """
workflows:
  standard:
    label: workflow:standard
    default: true
    path: [work, done]
"""
    )
    parsed = parse_workflow_file(path)
    assert parsed.config.workflows["standard"].triage is False


# ── Legacy-mode detection symmetry with workflows ───────────────────────────


def test_legacy_synthesis_symmetry_workflow_and_repo():
    """Legacy configs synthesize both _default workflow AND _default repo.

    Mirrors the existing multi-workflow _default pattern — a legacy config
    should have exactly one auto-generated entry in each of cfg.workflows
    and cfg.repos, both keyed '_default' with default=True.
    """
    path = _write_yaml(_MINIMAL_STATES)
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    # Workflow side (pre-existing behavior)
    assert len(cfg.workflows) == 1
    assert "_default" in cfg.workflows
    assert cfg.workflows["_default"].default is True

    # Repo side (new in this change)
    assert len(cfg.repos) == 1
    assert "_default" in cfg.repos
    assert cfg.repos["_default"].default is True
    assert cfg.repos_synthesized is True


# ── ServiceConfig.resolve_repo (Unit 2) ─────────────────────────────────────


def _cfg_with_repos(repos: list[tuple[str, str | None, bool]]) -> ServiceConfig:
    """Helper: build a ServiceConfig with the given (name, label, default) repos."""
    cfg = ServiceConfig()
    cfg.repos = {
        name: RepoConfig(name=name, label=label, clone_url=f"git@x/{name}", default=default)
        for name, label, default in repos
    }
    return cfg


def _issue(labels: list[str]) -> "Issue":
    from stokowski.models import Issue

    return Issue(id="x", identifier="TST-1", title="t", labels=labels)


def test_resolve_repo_label_match_wins():
    """resolve_repo: matching label wins over default."""
    cfg = _cfg_with_repos([("api", "repo:api", False), ("web", "repo:web", True)])
    assert cfg.resolve_repo(_issue(["repo:api"])).name == "api"


def test_resolve_repo_no_match_falls_back_to_default():
    """resolve_repo: no matching label → default-marked repo."""
    cfg = _cfg_with_repos([("api", "repo:api", False), ("web", "repo:web", True)])
    assert cfg.resolve_repo(_issue(["bug", "p1"])).name == "web"


def test_resolve_repo_no_labels_returns_default():
    """resolve_repo: issue with no labels at all returns default."""
    cfg = _cfg_with_repos([("api", "repo:api", False), ("web", "repo:web", True)])
    assert cfg.resolve_repo(_issue([])).name == "web"


def test_resolve_repo_case_insensitive_label_match():
    """resolve_repo: label matching is case-insensitive."""
    cfg = _cfg_with_repos([("api", "repo:api", True)])
    assert cfg.resolve_repo(_issue(["REPO:API"])).name == "api"


def test_resolve_repo_legacy_default_resolves():
    """Legacy synthesized _default repo resolves for any issue."""
    path = _write_yaml(_MINIMAL_STATES)
    parsed = parse_workflow_file(path)
    assert parsed.config.resolve_repo(_issue([])).name == "_default"
    assert parsed.config.resolve_repo(_issue(["unrelated"])).name == "_default"


def test_resolve_repo_no_default_raises():
    """resolve_repo: no match AND no default → ValueError."""
    cfg = _cfg_with_repos([("api", "repo:api", False)])
    with pytest.raises(ValueError, match="No default repo"):
        cfg.resolve_repo(_issue([]))


# ── Orchestrator repo routing + cache parity (Unit 2) ───────────────────────


class TestOrchestratorRepoRouting:
    """Tests for Orchestrator._resolve_repo, _get_issue_repo_config, and
    the cleanup contract for _issue_repo. Mirrors TestOrchestratorWorkflowRouting
    in test_state_machine.py.
    """

    def _make_orch(self, tmp_path):
        """Build an Orchestrator from a multi-repo workflow.yaml."""
        from stokowski.orchestrator import Orchestrator

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

repos:
  api:
    label: "repo:api"
    clone_url: "git@github.com:org/api.git"
  web:
    label: "repo:web"
    clone_url: "git@github.com:org/web.git"
    default: true
"""
        )
        orch = Orchestrator(str(wf_path))
        errors = orch._load_workflow()
        assert not errors, f"Config errors: {errors}"
        return orch

    @staticmethod
    def _make_issue(labels: list[str] | None = None, issue_id: str = "abc-1"):
        from stokowski.models import Issue

        return Issue(id=issue_id, identifier="TST-1", title="t", labels=labels or [])

    def test_resolve_repo_label_match_caches_name(self, tmp_path):
        """Matching repo:* label resolves correctly AND caches the name."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["repo:api"])

        repo = orch._resolve_repo(issue)
        assert repo.name == "api"
        assert orch._issue_repo[issue.id] == "api"

    def test_resolve_repo_no_label_returns_default(self, tmp_path):
        """Issue with no repo:* label resolves to default-marked repo."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["bug"])

        repo = orch._resolve_repo(issue)
        assert repo.name == "web"  # the default-marked repo in the fixture
        assert orch._issue_repo[issue.id] == "web"

    def test_get_issue_repo_config_cached(self, tmp_path):
        """_get_issue_repo_config returns the cached RepoConfig."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["repo:api"])
        orch._resolve_repo(issue)

        cached = orch._get_issue_repo_config(issue.id)
        assert cached.name == "api"

    def test_get_issue_repo_config_not_cached_returns_default(self, tmp_path):
        """Uncached issue id → default-marked repo."""
        orch = self._make_orch(tmp_path)
        assert orch._get_issue_repo_config("unknown-id").name == "web"

    def test_get_issue_repo_config_stale_cache_resolves_from_labels(self, tmp_path):
        """Hot-reload removes cached repo → re-resolves from labels.

        Mirrors the equivalent workflow test at test_state_machine.py:
        test_get_workflow_config_stale_cache_resolves_from_labels.
        """
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["repo:api"])
        orch._last_issues[issue.id] = issue
        orch._issue_repo[issue.id] = "api"  # simulate prior cache state

        # Simulate hot-reload removing the 'api' repo from the registry
        del orch.cfg.repos["api"]

        # Should fall back: cache miss for 'api' → re-resolve from labels.
        # Since labels still carry 'repo:api' but 'api' is gone, that fails too,
        # so we fall through to the default-marked repo.
        resolved = orch._get_issue_repo_config(issue.id)
        assert resolved.name == "web"  # default-marked repo in the fixture

    def test_get_issue_repo_config_stale_cache_no_issue_returns_default(self, tmp_path):
        """Stale cache AND no issue in _last_issues → default-marked repo."""
        orch = self._make_orch(tmp_path)
        orch._issue_repo["lost-id"] = "api"

        del orch.cfg.repos["api"]

        assert orch._get_issue_repo_config("lost-id").name == "web"

    def test_cleanup_removes_repo_entry(self, tmp_path):
        """_cleanup_issue_state removes the _issue_repo entry."""
        orch = self._make_orch(tmp_path)
        issue_id = "test-cleanup"

        orch._issue_repo[issue_id] = "api"
        orch._issue_workflow[issue_id] = "standard"
        orch._issue_current_state[issue_id] = "work"
        orch.claimed.add(issue_id)

        orch._cleanup_issue_state(issue_id)

        assert issue_id not in orch._issue_repo
        assert issue_id not in orch._issue_workflow
        assert issue_id not in orch._issue_current_state
        assert issue_id not in orch.claimed

    def test_cleanup_issue_repo_parity_with_init(self, tmp_path):
        """Meta-test: every per-issue dict/set in __init__ is popped by cleanup.

        Guards against the drift class called out in CLAUDE.md's pitfalls —
        if a new per-issue tracking structure is added to __init__ but not
        to _cleanup_issue_state, memory leaks and stale state result.
        """
        orch = self._make_orch(tmp_path)
        issue_id = "parity-test"

        # Pre-populate every per-issue dict/set that __init__ creates as empty.
        # If a new one is added later, this list must expand or the test fails.
        per_issue_dicts = [
            orch.running,
            orch._tasks,
            orch._retry_timers,
            orch.retry_attempts,
            orch._last_issues,
            orch._last_completed_at,
            orch._last_session_ids,
            orch._issue_current_state,
            orch._issue_state_runs,
            orch._pending_gates,
            orch._issue_workflow,
            orch._issue_repo,  # new in Unit 2
        ]
        per_issue_sets = [orch.claimed]

        for d in per_issue_dicts:
            # Use a sentinel value shape that matches each dict's real usage.
            # For timers we skip — real TimerHandle is complex — rely on dict pop.
            if d is orch._retry_timers:
                continue
            d[issue_id] = "sentinel"
        for s in per_issue_sets:
            s.add(issue_id)

        orch._cleanup_issue_state(issue_id)

        for d in per_issue_dicts:
            if d is orch._retry_timers:
                continue
            assert issue_id not in d, f"cleanup left entry in {d}"
        for s in per_issue_sets:
            assert issue_id not in s, f"cleanup left entry in {s}"
