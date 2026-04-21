"""State machine tracking via structured Linear comments."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("stokowski.tracking")

STATE_PATTERN = re.compile(r"<!-- stokowski:state ({.*?}) -->")
GATE_PATTERN = re.compile(r"<!-- stokowski:gate ({.*?}) -->")
REJECTED_PATTERN = re.compile(r"<!-- stokowski:rejected ({.*?}) -->")
MIGRATED_PATTERN = re.compile(r"<!-- stokowski:migrated ({.*?}) -->")


def make_state_comment(
    state: str,
    run: int = 1,
    workflow: str | None = None,
    repo: str | None = None,
) -> str:
    """Build a structured state-tracking comment.

    The optional ``repo`` field carries the name of the repo resolved at
    dispatch time. It's used by cold-start recovery to restore the repo
    assignment after an orchestrator restart. Pre-multi-repo comments
    have no ``repo`` field — readers MUST use ``payload.get("repo")``
    with a default, never direct subscript.
    """
    payload: dict[str, Any] = {
        "state": state,
        "run": run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if workflow is not None:
        payload["workflow"] = workflow
    if repo is not None:
        payload["repo"] = repo
    machine = f"<!-- stokowski:state {json.dumps(payload)} -->"
    if workflow is not None:
        human = (
            f"**[Stokowski]** Entering state: **{state}** "
            f"(workflow: {workflow}, run {run})"
        )
    else:
        human = f"**[Stokowski]** Entering state: **{state}** (run {run})"
    return f"{machine}\n\n{human}"


def make_gate_comment(
    state: str,
    status: str,
    prompt: str = "",
    rework_to: str | None = None,
    run: int = 1,
    workflow: str | None = None,
    repo: str | None = None,
) -> str:
    """Build a structured gate-tracking comment.

    See ``make_state_comment`` for the ``repo`` field contract.
    """
    payload: dict[str, Any] = {
        "state": state,
        "status": status,
        "run": run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if rework_to:
        payload["rework_to"] = rework_to
    if workflow is not None:
        payload["workflow"] = workflow
    if repo is not None:
        payload["repo"] = repo

    machine = f"<!-- stokowski:gate {json.dumps(payload)} -->"

    if status == "waiting":
        human = f"**[Stokowski]** Awaiting human review: **{state}**"
        if prompt:
            human += f" — {prompt}"
    elif status == "approved":
        human = f"**[Stokowski]** Gate **{state}** approved."
    elif status == "rework":
        human = (
            f"**[Stokowski]** Rework requested at **{state}**. "
            f"Returning to: **{rework_to}**"
        )
        if run > 1:
            human += f" (run {run})"
    elif status == "escalated":
        human = (
            f"**[Stokowski]** Max rework exceeded at **{state}**. "
            f"Escalating for human intervention."
        )
    else:
        human = f"**[Stokowski]** Gate **{state}** status: {status}"

    return f"{machine}\n\n{human}"


def make_rejection_comment(
    issue_labels: list[str],
    reason: str = "multi_repo",
) -> str:
    """Build a structured rejection comment for R10 enforcement.

    Used when a ticket violates the single-repo-per-ticket cap. The JSON
    payload carries the label set that triggered rejection — when labels
    change on a subsequent tick, the new label set won't match the
    sentinel, so ``has_pending_rejection`` returns False and a fresh
    rejection fires.

    Args:
        issue_labels: All labels currently on the issue; stored as a
            sorted, case-normalized list so comparisons are deterministic.
        reason: Short machine-readable reason. ``multi_repo`` is the
            default; ``triage_multi_repo`` distinguishes rejections
            triggered by a triage workflow's own label application.
    """
    normalized = sorted(l.lower() for l in issue_labels)
    payload: dict[str, Any] = {
        "labels": normalized,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    machine = f"<!-- stokowski:rejected {json.dumps(payload)} -->"

    if reason == "triage_multi_repo":
        human = (
            "**[Stokowski]** Triage applied two `repo:*` labels to this "
            "ticket. v1 supports at most one repo per ticket. Please "
            "resolve by removing one of the repo labels before dispatch "
            "can proceed."
        )
    else:
        repo_labels = [l for l in normalized if l.startswith("repo:")]
        human = (
            f"**[Stokowski]** Multiple `repo:*` labels detected "
            f"({', '.join(repo_labels)}). v1 supports at most one repo "
            f"per ticket. Please remove all but one of the `repo:*` "
            f"labels before dispatch can proceed."
        )

    return f"{machine}\n\n{human}"


def has_pending_rejection(
    comments: list[dict], current_labels: list[str]
) -> bool:
    """Return True if a rejection sentinel for the current label set exists.

    Scans comments for ``<!-- stokowski:rejected {...} -->`` markers and
    compares their ``labels`` payload against the current normalized
    label set. Used by the async rejection pre-pass to avoid spamming
    the comment thread with duplicate rejections on every poll tick.

    Label-change invalidation falls out naturally: if labels change,
    the new normalized sorted list won't match any prior sentinel,
    so a fresh rejection fires.
    """
    current = sorted(l.lower() for l in current_labels)
    for comment in comments:
        body = comment.get("body", "")
        match = REJECTED_PATTERN.search(body)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        prior_labels = data.get("labels", [])
        if prior_labels == current:
            return True
    return False


def make_migrated_comment(repo_name: str) -> str:
    """Build a cold-start migration notice comment.

    Emitted the first time the orchestrator picks up an issue whose
    tracking thread predates the multi-repo feature (no ``repo`` field
    in any prior state/gate comment). The issue is dispatched against
    the synthetic ``_default`` repo by default, unless the current
    ``repo:*`` label disagrees — in which case the worker surfaces an
    operator-facing warning separately and does not silently route.
    """
    payload: dict[str, Any] = {
        "from": "pre-repo-field",
        "using": repo_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    machine = f"<!-- stokowski:migrated {json.dumps(payload)} -->"
    human = (
        f"**[Stokowski]** Multi-repo upgrade migration: no `repo` field "
        f"found in prior tracking comments. Resuming against "
        f"`{repo_name}`. Change the `repo:*` label on this ticket if a "
        f"different repo is intended."
    )
    return f"{machine}\n\n{human}"


def parse_latest_tracking(comments: list[dict]) -> dict[str, Any] | None:
    """Parse comments (oldest-first) to find the latest state or gate tracking entry.

    Returns a dict with keys:
        - "type": "state" or "gate"
        - Plus all fields from the JSON payload

    Returns None if no tracking comments found.
    """
    latest: dict[str, Any] | None = None

    for comment in comments:
        body = comment.get("body", "")

        state_match = STATE_PATTERN.search(body)
        if state_match:
            try:
                data = json.loads(state_match.group(1))
                data["type"] = "state"
                data.setdefault("workflow", None)
                data.setdefault("repo", None)
                latest = data
            except json.JSONDecodeError:
                pass

        gate_match = GATE_PATTERN.search(body)
        if gate_match:
            try:
                data = json.loads(gate_match.group(1))
                data["type"] = "gate"
                data.setdefault("workflow", None)
                data.setdefault("repo", None)
                latest = data
            except json.JSONDecodeError:
                pass

    return latest


def get_last_tracking_timestamp(comments: list[dict]) -> str | None:
    """Find the timestamp of the latest tracking comment."""
    latest_ts: str | None = None

    for comment in comments:
        body = comment.get("body", "")
        for pattern in (STATE_PATTERN, GATE_PATTERN):
            match = pattern.search(body)
            if match:
                try:
                    data = json.loads(match.group(1))
                    ts = data.get("timestamp")
                    if ts:
                        latest_ts = ts
                except json.JSONDecodeError:
                    pass

    return latest_ts


def get_comments_since(
    comments: list[dict], since_timestamp: str | None
) -> list[dict]:
    """Filter comments to only those after a given timestamp.

    Returns comments that are NOT stokowski tracking comments and
    were created after the given timestamp.
    """
    result = []
    since_dt = None
    if since_timestamp:
        try:
            since_dt = datetime.fromisoformat(
                since_timestamp.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            pass

    for comment in comments:
        body = comment.get("body", "")
        if "<!-- stokowski:" in body:
            continue

        if since_dt:
            created = comment.get("createdAt", "")
            if created:
                try:
                    created_dt = datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    )
                    if created_dt <= since_dt:
                        continue
                except (ValueError, AttributeError):
                    pass

        result.append(comment)

    return result
