"""Tests for the async rejection pre-pass and cold-start repo recovery
(Unit 7 orchestrator-side).

These tests exercise Orchestrator._process_rejections and
Orchestrator._resolve_repo_for_coldstart with stubbed LinearClient
interactions (no real network calls).
"""

from __future__ import annotations

import asyncio

import pytest

from stokowski.models import Issue
from stokowski.orchestrator import Orchestrator
from stokowski.tracking import (
    make_gate_comment,
    make_migrated_comment,
    make_rejection_comment,
    make_state_comment,
    REJECTED_PATTERN,
    MIGRATED_PATTERN,
)


def _run(coro):
    return asyncio.run(coro)


class _StubLinearClient:
    """In-memory stand-in for LinearClient used by rejection + cold-start tests.

    Tracks posted comments per issue_id and supports preloading a comment
    thread to be returned from fetch_comments.
    """

    def __init__(self):
        self.posted: dict[str, list[str]] = {}
        self.preloaded: dict[str, list[dict]] = {}

    async def fetch_comments(self, issue_id: str) -> list[dict]:
        # Return preloaded thread plus any posted-during-test comments
        preloaded = list(self.preloaded.get(issue_id, []))
        posted_now = [
            {"body": body, "createdAt": "2026-04-20T00:00:00Z"}
            for body in self.posted.get(issue_id, [])
        ]
        return preloaded + posted_now

    async def post_comment(self, issue_id: str, body: str) -> bool:
        self.posted.setdefault(issue_id, []).append(body)
        return True

    async def close(self):
        pass


def _make_orch(tmp_path):
    """Standard multi-repo orchestrator fixture."""
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


def _issue(labels: list[str], issue_id: str = "iid-1"):
    return Issue(id=issue_id, identifier="SMI-1", title="t", labels=labels)


# ── Rejection pre-pass ──────────────────────────────────────────────────────


def test_rejection_prepass_single_repo_label_no_marker(tmp_path):
    """One repo:* label → no rejection, dispatch proceeds."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    _run(orch._process_rejections([_issue(["repo:api"])]))

    assert "iid-1" not in orch._rejected_issues
    assert orch._linear.posted == {}


def test_rejection_prepass_dual_repo_labels_marks_rejected(tmp_path):
    """Two repo:* labels → marker added, rejection comment posted."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    _run(orch._process_rejections([_issue(["repo:api", "repo:web"])]))

    assert "iid-1" in orch._rejected_issues
    assert len(orch._linear.posted["iid-1"]) == 1
    assert REJECTED_PATTERN.search(orch._linear.posted["iid-1"][0])


def test_rejection_prepass_dedup_across_ticks(tmp_path):
    """Second tick with same dual labels posts no new rejection comment."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()
    issue = _issue(["repo:api", "repo:web"])

    _run(orch._process_rejections([issue]))
    first_count = len(orch._linear.posted["iid-1"])

    # Simulate the posted comment now existing in the comment thread
    orch._linear.preloaded["iid-1"] = [
        {"body": c, "createdAt": "2026-04-20T00:00:00Z"}
        for c in orch._linear.posted["iid-1"]
    ]
    orch._linear.posted = {}  # reset to count new posts

    # Clear the in-memory marker (as if orchestrator restarted)
    orch._rejected_issues.discard("iid-1")

    _run(orch._process_rejections([issue]))

    # has_pending_rejection should have caught the existing sentinel →
    # no new comment posted. Marker is re-populated.
    assert "iid-1" not in orch._linear.posted  # no new post
    assert "iid-1" in orch._rejected_issues


def test_rejection_prepass_label_change_invalidates_marker(tmp_path):
    """When labels change to a new dual-label set, the sentinel doesn't match
    and a fresh rejection fires."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    # Tick 1: dual labels {api, web}
    orch._last_issues["iid-1"] = _issue(["repo:api", "repo:web"])
    _run(orch._process_rejections([_issue(["repo:api", "repo:web"])]))
    first_count = len(orch._linear.posted.get("iid-1", []))

    # Preload existing comment into the thread
    orch._linear.preloaded["iid-1"] = [
        {"body": c, "createdAt": "2026-04-20T00:00:00Z"}
        for c in orch._linear.posted.get("iid-1", [])
    ]
    orch._linear.posted = {}

    # Tick 2: labels change to {api, mobile}
    new_issue = _issue(["repo:api", "repo:mobile"])
    orch._last_issues["iid-1"] = _issue(["repo:api", "repo:web"])  # prior state
    _run(orch._process_rejections([new_issue]))

    # Stale marker discarded, new rejection posted
    assert "iid-1" in orch._rejected_issues
    assert "iid-1" in orch._linear.posted
    assert len(orch._linear.posted["iid-1"]) == 1


def test_rejection_prepass_labels_fixed_clears_marker(tmp_path):
    """Operator removes one of two labels → marker discarded, dispatch OK."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    # Start rejected
    orch._rejected_issues.add("iid-1")

    # Tick sees only one repo label now
    _run(orch._process_rejections([_issue(["repo:api"])]))

    assert "iid-1" not in orch._rejected_issues


def test_rejection_prepass_no_repo_labels_no_marker(tmp_path):
    """Zero repo:* labels is fine — dispatch will route via default."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    _run(orch._process_rejections([_issue(["bug", "p1"])]))

    assert "iid-1" not in orch._rejected_issues
    assert orch._linear.posted == {}


def test_rejection_prepass_triage_origin_detection(tmp_path):
    """Dual labels on a ticket whose most recent state comment was from
    a triage workflow → rejection reason is triage_multi_repo."""
    # Rebuild orch with a triage workflow defined
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
    assert not errors

    stub = _StubLinearClient()
    orch._linear = stub

    # Preload a state comment from the triage workflow (the ticket came out
    # of triage, then triage labeled it with two repos — human or bug)
    triage_state_comment = make_state_comment(
        state="work", run=1, workflow="intake", repo="_default",
    )
    stub.preloaded["iid-1"] = [
        {"body": triage_state_comment, "createdAt": "2026-04-20T00:00:00Z"}
    ]

    _run(orch._process_rejections([_issue(["repo:api", "repo:web"])]))

    # Rejection should be tagged as triage-originated
    assert "iid-1" in orch._rejected_issues
    posted = stub.posted["iid-1"][0]
    assert "triage_multi_repo" in posted
    assert "Triage applied two" in posted


# ── Cold-start recovery ─────────────────────────────────────────────────────


def test_coldstart_cache_already_populated_noop(tmp_path):
    """If _issue_repo already has the entry, cold-start is a no-op."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()
    orch._issue_repo["iid-1"] = "api"

    issue = _issue(["repo:api"])
    _run(orch._resolve_repo_for_coldstart(issue, tracking=None, comments=[]))

    # Unchanged
    assert orch._issue_repo["iid-1"] == "api"
    assert orch._linear.posted == {}


def test_coldstart_tracking_repo_field_restored(tmp_path):
    """Tracking comment with repo field → cache populated, no migration post."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    issue = _issue(["repo:api"])
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": "api"}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))

    assert orch._issue_repo["iid-1"] == "api"
    assert orch._linear.posted == {}


def test_coldstart_tracking_repo_missing_falls_back_to_labels(tmp_path):
    """Pre-migration tracking (no repo field) → resolve via labels, no migrated
    post because we resolved to a non-default repo."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    issue = _issue(["repo:api"])
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": None}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))

    assert orch._issue_repo["iid-1"] == "api"
    # No migrated comment — fell back to an explicit repo, not _default
    assert orch._linear.posted == {}


def test_coldstart_tracking_repo_missing_defaults_to_default_posts_migrated(tmp_path):
    """Pre-migration tracking + no repo label → _default + migrated notice."""
    # Swap to a config where synthesized _default would apply (no repos: section)
    legacy_wf = tmp_path / "legacy.yaml"
    legacy_wf.write_text(
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
"""
    )
    orch = Orchestrator(str(legacy_wf))
    errors = orch._load_workflow()
    assert not errors
    orch._linear = _StubLinearClient()

    issue = _issue([])
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": None}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))

    assert orch._issue_repo["iid-1"] == "_default"
    # Migrated comment posted
    assert "iid-1" in orch._linear.posted
    assert MIGRATED_PATTERN.search(orch._linear.posted["iid-1"][0])
    assert "iid-1" in orch._migrated_issues


def test_coldstart_migrated_posted_only_once(tmp_path):
    """Repeated cold-start calls on the same issue don't spam migrated."""
    legacy_wf = tmp_path / "legacy.yaml"
    legacy_wf.write_text(
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
"""
    )
    orch = Orchestrator(str(legacy_wf))
    orch._load_workflow()
    orch._linear = _StubLinearClient()

    issue = _issue([])
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": None}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))
    first = list(orch._linear.posted.get("iid-1", []))

    # Clear cache to simulate a fresh call that triggers cold-start again
    orch._issue_repo.clear()
    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))
    second = list(orch._linear.posted.get("iid-1", []))

    assert len(second) == len(first)  # no additional migration posts


def test_coldstart_tracking_points_to_removed_repo_falls_back(tmp_path):
    """Tracking names a repo that no longer exists in config → label resolve."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    issue = _issue(["repo:api"])
    # Tracking names a repo that doesn't exist (hot-reload removed it)
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": "removed-repo"}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))

    # Falls back to label resolution
    assert orch._issue_repo["iid-1"] == "api"


# ── Cleanup parity (Unit 2 meta-test extended) ──────────────────────────────


def test_cleanup_removes_rejected_and_migrated_markers(tmp_path):
    """_cleanup_issue_state removes the new _rejected_issues and
    _migrated_issues entries (parity with __init__)."""
    orch = _make_orch(tmp_path)
    issue_id = "cleanup-test"
    orch._rejected_issues.add(issue_id)
    orch._migrated_issues.add(issue_id)

    orch._cleanup_issue_state(issue_id)

    assert issue_id not in orch._rejected_issues
    assert issue_id not in orch._migrated_issues
