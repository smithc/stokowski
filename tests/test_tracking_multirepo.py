"""Tests for multi-repo additions to tracking comments (Unit 7).

Covers:
- make_state_comment / make_gate_comment carrying the repo field
- parse_latest_tracking defensive read (.get with None default)
- make_rejection_comment payload + has_pending_rejection dedup
- make_migrated_comment payload
"""

from __future__ import annotations

import json

from stokowski.tracking import (
    GATE_PATTERN,
    MIGRATED_PATTERN,
    REJECTED_PATTERN,
    STATE_PATTERN,
    has_pending_rejection,
    make_gate_comment,
    make_migrated_comment,
    make_rejection_comment,
    make_state_comment,
    parse_latest_tracking,
)


# ── make_state_comment: repo field ──────────────────────────────────────────


def test_state_comment_includes_repo_field():
    body = make_state_comment(state="plan", run=1, workflow="std", repo="api")
    match = STATE_PATTERN.search(body)
    assert match is not None
    data = json.loads(match.group(1))
    assert data["repo"] == "api"


def test_state_comment_omits_repo_when_none():
    """Backward compat: repo=None omits the field entirely from the payload."""
    body = make_state_comment(state="plan", run=1, workflow="std")
    match = STATE_PATTERN.search(body)
    data = json.loads(match.group(1))
    assert "repo" not in data


# ── make_gate_comment: repo field ───────────────────────────────────────────


def test_gate_comment_includes_repo_field():
    body = make_gate_comment(
        state="review", status="waiting", run=1, workflow="std", repo="api",
    )
    match = GATE_PATTERN.search(body)
    data = json.loads(match.group(1))
    assert data["repo"] == "api"


def test_gate_comment_omits_repo_when_none():
    body = make_gate_comment(state="review", status="waiting", run=1)
    match = GATE_PATTERN.search(body)
    data = json.loads(match.group(1))
    assert "repo" not in data


# ── parse_latest_tracking: defensive read ───────────────────────────────────


def test_parse_latest_tracking_sets_repo_none_for_legacy_state_comment():
    """Pre-migration state comments (no repo field) must not crash readers."""
    body = make_state_comment(state="plan", run=1, workflow="std")
    comments = [{"body": body, "createdAt": "2026-01-01T00:00:00Z"}]

    tracking = parse_latest_tracking(comments)
    assert tracking is not None
    assert tracking["type"] == "state"
    assert tracking["workflow"] == "std"
    # Defensive default: repo is exposed as None rather than missing key
    assert tracking["repo"] is None


def test_parse_latest_tracking_exposes_repo_when_present():
    body = make_state_comment(
        state="plan", run=1, workflow="std", repo="api",
    )
    comments = [{"body": body, "createdAt": "2026-01-01T00:00:00Z"}]
    tracking = parse_latest_tracking(comments)
    assert tracking["repo"] == "api"


def test_parse_latest_tracking_legacy_gate_comment_repo_none():
    body = make_gate_comment(state="review", status="waiting", workflow="std")
    comments = [{"body": body, "createdAt": "2026-01-01T00:00:00Z"}]
    tracking = parse_latest_tracking(comments)
    assert tracking["type"] == "gate"
    assert tracking["repo"] is None


def test_parse_latest_tracking_get_with_default_pattern():
    """Regression guard: readers should use the `.get('repo') or fallback`
    pattern, never direct subscript. ``parse_latest_tracking`` uses
    ``setdefault("repo", None)`` so the key is always present — the value
    is None when the payload didn't carry the field. Readers must treat
    None as "absent" via ``or``-style fallback.

    Models the cold-start helper's access pattern.
    """
    body = make_state_comment(state="plan", run=1, workflow="std")
    comments = [{"body": body, "createdAt": "2026-01-01T00:00:00Z"}]
    tracking = parse_latest_tracking(comments)
    # Direct .get() returns None (setdefault stored it):
    assert tracking.get("repo") is None
    # Correct reader pattern: `.get() or fallback`:
    assert (tracking.get("repo") or "_default") == "_default"
    # Must never KeyError (regression guard on parse_latest_tracking)
    assert "repo" in tracking  # key is always present, value may be None


# ── make_rejection_comment + has_pending_rejection ──────────────────────────


def test_rejection_comment_includes_label_set():
    body = make_rejection_comment(["repo:api", "repo:web", "bug"])
    match = REJECTED_PATTERN.search(body)
    assert match is not None
    data = json.loads(match.group(1))
    # Labels are sorted and case-normalized
    assert data["labels"] == ["bug", "repo:api", "repo:web"]
    assert data["reason"] == "multi_repo"


def test_rejection_comment_default_reason():
    body = make_rejection_comment(["repo:api", "repo:web"])
    match = REJECTED_PATTERN.search(body)
    data = json.loads(match.group(1))
    assert data["reason"] == "multi_repo"


def test_rejection_comment_triage_reason():
    body = make_rejection_comment(
        ["repo:api", "repo:web"], reason="triage_multi_repo",
    )
    match = REJECTED_PATTERN.search(body)
    data = json.loads(match.group(1))
    assert data["reason"] == "triage_multi_repo"
    assert "Triage applied two" in body


def test_rejection_comment_human_text_lists_repo_labels():
    body = make_rejection_comment(
        ["bug", "repo:api", "repo:web"],
    )
    assert "repo:api" in body
    assert "repo:web" in body
    assert "bug" not in body.split("\n\n", 1)[1]  # human part only lists repos


def test_has_pending_rejection_true_for_matching_label_set():
    rejection_body = make_rejection_comment(["repo:api", "repo:web"])
    comments = [{"body": rejection_body}]

    assert has_pending_rejection(comments, ["repo:api", "repo:web"]) is True


def test_has_pending_rejection_case_insensitive_match():
    rejection_body = make_rejection_comment(["repo:api", "repo:web"])
    comments = [{"body": rejection_body}]

    # Different case in the current labels — still matches the stored set
    assert has_pending_rejection(comments, ["REPO:API", "Repo:Web"]) is True


def test_has_pending_rejection_false_when_label_set_changed():
    """Key dedup property: a new label set invalidates the prior sentinel."""
    rejection_body = make_rejection_comment(["repo:api", "repo:web"])
    comments = [{"body": rejection_body}]

    # Operator changed one label — different set, should re-fire
    assert has_pending_rejection(comments, ["repo:api", "repo:mobile"]) is False


def test_has_pending_rejection_false_when_no_sentinel():
    """No rejection sentinel in comments → False."""
    state_body = make_state_comment(state="plan", run=1, workflow="std")
    comments = [{"body": state_body}]
    assert has_pending_rejection(comments, ["repo:api", "repo:web"]) is False


def test_has_pending_rejection_false_for_empty_comments():
    assert has_pending_rejection([], ["repo:api"]) is False


def test_has_pending_rejection_tolerates_malformed_json():
    """A malformed sentinel doesn't crash — just skipped."""
    bad_body = "<!-- stokowski:rejected {malformed-json} -->"
    comments = [{"body": bad_body}]
    assert has_pending_rejection(comments, ["repo:api"]) is False


def test_has_pending_rejection_sorts_before_compare():
    """Input order on current_labels doesn't matter — sentinels store sorted."""
    rejection_body = make_rejection_comment(["repo:web", "repo:api"])
    comments = [{"body": rejection_body}]

    assert has_pending_rejection(comments, ["repo:api", "repo:web"]) is True


# ── make_migrated_comment ───────────────────────────────────────────────────


def test_migrated_comment_payload():
    body = make_migrated_comment("_default")
    match = MIGRATED_PATTERN.search(body)
    assert match is not None
    data = json.loads(match.group(1))
    assert data["from"] == "pre-repo-field"
    assert data["using"] == "_default"


def test_migrated_comment_human_text():
    body = make_migrated_comment("_default")
    assert "Multi-repo upgrade migration" in body
    assert "_default" in body
