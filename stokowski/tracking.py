"""State machine tracking via structured Linear comments."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, TypeAlias

# Sort-key type for watermark supersession ordering in _fired_sort_key.
_SortKey: TypeAlias = tuple[tuple, int, int]

logger = logging.getLogger("stokowski.tracking")

STATE_PATTERN = re.compile(r"<!-- stokowski:state ({.*?}) -->")
GATE_PATTERN = re.compile(r"<!-- stokowski:gate ({.*?}) -->")
REJECTED_PATTERN = re.compile(r"<!-- stokowski:rejected ({.*?}) -->")
MIGRATED_PATTERN = re.compile(r"<!-- stokowski:migrated ({.*?}) -->")
FIRED_PATTERN = re.compile(r"<!-- stokowski:fired ({.*?}) -->")
BOUNDED_DROP_PATTERN = re.compile(r"<!-- stokowski:bounded_drop ({.*?}) -->")
SCHEDULE_ERROR_PATTERN = re.compile(r"<!-- stokowski:schedule_error ({.*?}) -->")
CANCELED_PATTERN = re.compile(r"<!-- stokowski:canceled ({.*?}) -->")
CANCELED_REF_PATTERN = re.compile(r"<!-- stokowski:canceled_ref ({.*?}) -->")

# Known watermark status values (for reference; parsers accept anything for
# forward compat, orchestrator is the canonical consumer).
FIRED_STATUSES = frozenset(
    {
        "pending",
        "child",
        "failed",
        "failed_permanent",
        "skipped_overlap",
        "skipped_bounded",
        "skipped_paused",
        "skipped_error",
    }
)


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


# ---------------------------------------------------------------------------
# Scheduled-job watermarks (stokowski:fired)
# ---------------------------------------------------------------------------


def make_fired_comment(
    template_id: str,
    slot: str,
    status: str,
    *,
    child_id: str | None = None,
    attempt: int | None = None,
    reason: str | None = None,
    seq: int | None = None,
    timestamp: str | None = None,
) -> str:
    """Build a structured fire-watermark comment.

    Posted on a template issue to mark progress through the fire protocol
    for a given `slot`. Statuses: pending, child, failed, failed_permanent,
    skipped_overlap, skipped_bounded, skipped_paused, skipped_error.

    Parameters
    ----------
    template_id:
        The template issue identifier (or id) this watermark belongs to.
    slot:
        Canonicalized slot key (ISO-8601 UTC for cron, `trigger:<id>` for
        Trigger-Now).
    status:
        One of the values in `FIRED_STATUSES`.
    child_id:
        The created child issue identifier (set when status=="child").
    attempt:
        Retry counter (set when status in {"pending", "failed",
        "failed_permanent"}).
    reason:
        Short machine-readable reason code (set on failure / skip).
    seq:
        Monotonic per-template counter for tie-breaking identical
        timestamps. Orchestrator owns the counter; this function simply
        embeds whatever value it is given.
    timestamp:
        Override the auto-generated UTC ISO-8601 timestamp (mostly useful
        for tests).
    """
    payload: dict[str, Any] = {
        "template": template_id,
        "slot": slot,
        "status": status,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }
    if child_id is not None:
        payload["child"] = child_id
    if attempt is not None:
        payload["attempt"] = attempt
    if reason is not None:
        payload["reason"] = reason
    if seq is not None:
        payload["seq"] = seq

    machine = f"<!-- stokowski:fired {json.dumps(payload)} -->"

    # Human-readable line. Kept short; human reviewers mostly read the
    # machine payload via dashboard, but Linear renders markdown so the
    # line is useful in the comments timeline.
    if status == "child" and child_id:
        human = (
            f"**[Stokowski]** Fire slot `{slot}`: created child "
            f"**{child_id}**."
        )
    elif status == "pending":
        attempt_txt = f" (attempt {attempt})" if attempt is not None else ""
        human = (
            f"**[Stokowski]** Fire slot `{slot}`: preparing child"
            f"{attempt_txt}."
        )
    elif status == "failed":
        attempt_txt = f" (attempt {attempt})" if attempt is not None else ""
        reason_txt = f" reason: {reason}" if reason else ""
        human = (
            f"**[Stokowski]** Fire slot `{slot}`: failed{attempt_txt}."
            f"{reason_txt}"
        )
    elif status == "failed_permanent":
        reason_txt = f" reason: {reason}" if reason else ""
        human = (
            f"**[Stokowski]** Fire slot `{slot}`: permanently failed."
            f"{reason_txt}"
        )
    elif status.startswith("skipped_"):
        skip_kind = status[len("skipped_"):]
        reason_txt = f" — {reason}" if reason else ""
        human = (
            f"**[Stokowski]** Fire slot `{slot}`: skipped "
            f"({skip_kind}){reason_txt}."
        )
    else:
        human = (
            f"**[Stokowski]** Fire slot `{slot}`: status **{status}**."
        )

    return f"{machine}\n\n{human}"


def _parse_fired_payload(body: str) -> dict[str, Any] | None:
    """Return the parsed watermark JSON payload or None if not found/invalid."""
    match = FIRED_PATTERN.search(body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.debug("Skipping malformed stokowski:fired payload")
        return None
    if not isinstance(data, dict):
        return None
    return data


def _fired_sort_key(entry: dict[str, Any], index: int) -> _SortKey:
    """Sort key for watermark supersession.

    Primary: timestamp (lexicographic ISO-8601 sort works when all entries
    are UTC with the same precision; fall back to parsed datetime when
    possible to tolerate mixed precisions).
    Secondary: seq ascending (higher wins because we take the last).
    Tertiary: input index (later-in-list wins → matches oldest-first
    "last wins" convention).
    """
    ts = entry.get("timestamp", "")
    ts_dt: datetime | None = None
    if isinstance(ts, str) and ts:
        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            ts_dt = None
    seq = entry.get("seq")
    try:
        seq_val = int(seq) if seq is not None else -1
    except (TypeError, ValueError):
        seq_val = -1
    # Use epoch seconds when parseable; otherwise fall back to the raw
    # string which still gives a stable relative ordering for identically
    # formatted timestamps.
    ts_key: tuple
    if ts_dt is not None:
        ts_key = (0, ts_dt.timestamp())
    else:
        ts_key = (1, ts if isinstance(ts, str) else "")
    return (ts_key, seq_val, index)


def parse_latest_fired(comments: list[dict]) -> dict[str, Any] | None:
    """Return the latest fire watermark across all slots for this template.

    Comments are assumed oldest-first (Linear `orderBy: createdAt`).
    Uses timestamp → seq → input-order tiebreak so that two watermarks
    written in the same millisecond are ordered by their monotonic `seq`.
    Returns None if no watermark comments are present.
    """
    entries: list[tuple[tuple, dict[str, Any]]] = []
    for index, comment in enumerate(comments):
        body = comment.get("body", "") if isinstance(comment, dict) else ""
        if not body:
            continue
        payload = _parse_fired_payload(body)
        if payload is None:
            continue
        entries.append((_fired_sort_key(payload, index), payload))

    if not entries:
        return None
    entries.sort(key=lambda item: item[0])
    return entries[-1][1]


def parse_fired_by_slot(comments: list[dict]) -> dict[str, dict[str, Any]]:
    """Return `{slot: latest_watermark}` for every slot seen in comments.

    Comments are assumed oldest-first; per-slot tiebreak uses the same
    timestamp → seq → input-index ordering as `parse_latest_fired`.
    Watermarks without a `slot` field are ignored. An empty input returns
    an empty dict.
    """
    per_slot: dict[str, list[tuple[tuple, dict[str, Any]]]] = {}
    for index, comment in enumerate(comments):
        body = comment.get("body", "") if isinstance(comment, dict) else ""
        if not body:
            continue
        payload = _parse_fired_payload(body)
        if payload is None:
            continue
        slot = payload.get("slot")
        if not isinstance(slot, str) or not slot:
            continue
        per_slot.setdefault(slot, []).append(
            (_fired_sort_key(payload, index), payload)
        )

    result: dict[str, dict[str, Any]] = {}
    for slot, entries in per_slot.items():
        entries.sort(key=lambda item: item[0])
        result[slot] = entries[-1][1]
    return result


# ---------------------------------------------------------------------------
# Bounded-drop surface (I5)
# ---------------------------------------------------------------------------


def make_bounded_drop_comment(
    template_id: str,
    dropped_count: int,
    earliest_slot: str,
    latest_slot: str,
    *,
    timestamp: str | None = None,
) -> str:
    """Build a bounded-drop surface comment.

    Posted when `on_missed: run_all` exceeds the configured cap and the
    orchestrator had to discard the earliest missed slots.
    """
    payload: dict[str, Any] = {
        "template": template_id,
        "dropped_count": dropped_count,
        "earliest_slot": earliest_slot,
        "latest_slot": latest_slot,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }
    machine = f"<!-- stokowski:bounded_drop {json.dumps(payload)} -->"
    human = (
        f"**[Stokowski]** Dropped **{dropped_count}** missed fire slots "
        f"(from `{earliest_slot}` through `{latest_slot}`) after exceeding "
        f"the run_all backfill cap."
    )
    return f"{machine}\n\n{human}"


# ---------------------------------------------------------------------------
# Schedule-error surface (R18)
# ---------------------------------------------------------------------------


def make_schedule_error_comment(
    template_id: str,
    reason: str,
    details: str | None = None,
    *,
    timestamp: str | None = None,
) -> str:
    """Build a schedule-error comment posted when a template is moved to Error.

    Idempotent callers consult `parse_latest_schedule_error` and only post
    on a distinct `reason`.
    """
    payload: dict[str, Any] = {
        "template": template_id,
        "reason": reason,
        "details": details,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }
    machine = f"<!-- stokowski:schedule_error {json.dumps(payload)} -->"
    if details:
        human = (
            f"**[Stokowski]** Schedule error (`{reason}`): {details}"
        )
    else:
        human = f"**[Stokowski]** Schedule error: `{reason}`."
    return f"{machine}\n\n{human}"


def parse_latest_schedule_error(
    comments: list[dict],
) -> dict[str, Any] | None:
    """Return the latest schedule-error comment payload, or None.

    Oldest-first "last wins" scan, matching `parse_latest_tracking`.
    """
    latest: dict[str, Any] | None = None
    for comment in comments:
        body = comment.get("body", "") if isinstance(comment, dict) else ""
        if not body:
            continue
        match = SCHEDULE_ERROR_PATTERN.search(body)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.debug("Skipping malformed stokowski:schedule_error payload")
            continue
        if isinstance(data, dict):
            latest = data
    return latest


# ---------------------------------------------------------------------------
# Cancel-previous surface (R8)
# ---------------------------------------------------------------------------


def make_cancel_comment(
    child_id: str,
    reason: str,
    triggering_slot: str,
    template_id: str,
    *,
    already_terminaled: bool = False,
    replacement_child_id: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Build a cancel-previous tracking comment posted on the CHILD issue.

    This is the primary reviewer notification for the
    ``overlap_policy: cancel_previous`` flow (R8). The child may be
    actively running, awaiting gate review, or (rarely) already terminal
    by the time this comment is posted; the ``already_terminaled`` flag
    preserves the audit signal without overwriting a naturally-terminated
    child's state.
    """
    payload: dict[str, Any] = {
        "child": child_id,
        "template": template_id,
        "reason": reason,
        "triggering_slot": triggering_slot,
        "already_terminaled": bool(already_terminaled),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }
    if replacement_child_id is not None:
        payload["replacement_child"] = replacement_child_id

    machine = f"<!-- stokowski:canceled {json.dumps(payload)} -->"
    if already_terminaled:
        human = (
            f"**[Stokowski]** Cancel requested for slot `{triggering_slot}` "
            f"on template **{template_id}** — this child already terminaled "
            f"naturally; no state change applied."
        )
    elif replacement_child_id:
        human = (
            f"**[Stokowski]** Canceling this run to make way for fire slot "
            f"`{triggering_slot}` on template **{template_id}**. "
            f"Replacement child: **{replacement_child_id}**."
        )
    else:
        human = (
            f"**[Stokowski]** Canceling this run (reason: `{reason}`) — "
            f"triggered by slot `{triggering_slot}` on template **{template_id}**."
        )
    return f"{machine}\n\n{human}"


def make_cancel_reference_comment(
    template_id: str,
    canceled_child_identifier: str,
    triggering_slot: str,
    *,
    already_terminaled: bool = False,
    timestamp: str | None = None,
) -> str:
    """Build a short reference comment posted on the TEMPLATE.

    Audit-trail only — the full cancel surface lives on the child (see
    ``make_cancel_comment``). Kept compact to avoid cluttering the
    template's comments timeline.
    """
    payload: dict[str, Any] = {
        "template": template_id,
        "canceled_child": canceled_child_identifier,
        "triggering_slot": triggering_slot,
        "already_terminaled": bool(already_terminaled),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }
    machine = f"<!-- stokowski:canceled_ref {json.dumps(payload)} -->"
    if already_terminaled:
        human = (
            f"**[Stokowski]** Slot `{triggering_slot}` attempted to cancel "
            f"**{canceled_child_identifier}** — already terminaled naturally."
        )
    else:
        human = (
            f"**[Stokowski]** Slot `{triggering_slot}` canceled previous "
            f"child **{canceled_child_identifier}**."
        )
    return f"{machine}\n\n{human}"


def parse_latest_cancel(comments: list[dict]) -> dict[str, Any] | None:
    """Return the latest cancel-previous payload posted on a child, or None.

    Oldest-first "last wins" scan. Used by tests (and optionally the
    dashboard) to verify the cancel protocol posted the expected payload.
    """
    latest: dict[str, Any] | None = None
    for comment in comments:
        body = comment.get("body", "") if isinstance(comment, dict) else ""
        if not body:
            continue
        match = CANCELED_PATTERN.search(body)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.debug("Skipping malformed stokowski:canceled payload")
            continue
        if isinstance(data, dict):
            latest = data
    return latest
