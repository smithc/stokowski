"""Pure schedule evaluator for scheduled-job templates.

This module is I/O-free and mutation-free. Given snapshots of the current
template state, recent watermark history, in-flight child count, and the
current time, it returns a list of ``FireDecision`` records describing what
the orchestrator should do next for a given template.

Purity invariants (enforced by discipline, not the type system):

- No ``datetime.now()`` calls — always take ``now`` as a parameter.
- No Linear / Docker / filesystem I/O.
- Inputs are not mutated. Outputs are freshly constructed.
- No module-level mutable state.

The orchestrator wraps this module to perform the side effects (posting
watermarks, creating children, recording fire attempts). Keeping the
evaluator pure matches the ``cleanup_old_logs`` / ``enforce_size_limit``
pattern and lets the tests run with no mocks or network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .config import ScheduleConfig


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CronParseError(Exception):
    """Raised when a cron expression cannot be parsed by croniter."""


class TimezoneError(Exception):
    """Raised when a timezone name cannot be resolved via zoneinfo."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateSnapshot:
    """Immutable snapshot of a template issue at evaluation time.

    Fields are the minimum the evaluator needs. Callers build this from a
    Linear ``Issue`` plus custom-field reads (cron, timezone) resolved by
    Unit 2. ``linear_state`` is the human-readable Linear state name
    (e.g. ``"Scheduled"``, ``"Paused"``, ``"Trigger Now"``, ``"Error"``).
    """

    id: str
    identifier: str
    linear_state: str
    cron_expr: str
    timezone: str
    labels: tuple[str, ...] = ()
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Watermark:
    """Parsed `<!-- stokowski:fired ... -->` tracking comment.

    The evaluator only reads the fields it needs for decisions; richer
    metadata (e.g. ``reason``) can still be present in the source comment.
    """

    template_id: str
    slot: str
    status: str  # pending | child | failed | failed_permanent | skipped_*
    child_id: Optional[str] = None
    attempt: Optional[int] = None
    reason: Optional[str] = None
    timestamp: Optional[datetime] = None
    seq: int = 0


FireAction = Literal[
    "fire",
    "skip_overlap",
    "skip_bounded",
    "skip_paused",
    "skip_error",
]


@dataclass(frozen=True)
class FireDecision:
    """A single evaluator decision for one (template, slot) pair.

    ``slot`` is always the canonical serialized form (see
    :func:`canonicalize_slot`). For Trigger-Now fires, ``slot`` is prefixed
    with ``trigger:`` and ``is_trigger_now`` is True.
    """

    template_id: str
    slot: str
    action: FireAction
    reason: Optional[str] = None
    is_trigger_now: bool = False
    # Number of slots dropped by ``run_all`` bound exceeded. Populated only
    # on ``skip_bounded`` decisions that carry the aggregate drop count.
    bounded_dropped_count: int = 0
    # Window of the bounded drop (for the aggregate bounded_drop comment).
    bounded_drop_earliest: Optional[str] = None
    bounded_drop_latest: Optional[str] = None


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def canonicalize_slot(dt: datetime) -> str:
    """Canonical serialization for slot timestamps.

    Produces ISO-8601 at second precision with a trailing ``Z`` for UTC.
    Sub-second precision is dropped — watermark comments and the
    ``slot:<ISO>`` child label MUST round-trip through this function so
    the evaluator's idempotency invariant holds across modules.

    The caller is responsible for converting ``dt`` to UTC before passing
    it in; the slot value is always serialized UTC regardless of the
    template's configured timezone (per plan Key Decisions).

    >>> from datetime import datetime, timezone
    >>> canonicalize_slot(datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc))
    '2026-04-19T08:00:00Z'
    """
    if dt.tzinfo is None:
        # Naive datetime — interpret as UTC. Callers shouldn't pass these,
        # but being permissive here avoids a class of confusing bugs at
        # API boundaries.
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    dt = dt.replace(microsecond=0)
    # isoformat() emits "+00:00"; the "Z" form is the canonical token we
    # promise to the orchestrator and Linear child labels.
    return dt.isoformat().replace("+00:00", "Z")


def _trigger_slot(now: datetime) -> str:
    return f"trigger:{canonicalize_slot(now)}"


# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------


def detect_paused(template_state: str) -> bool:
    """Return True if the template's linear state means paused."""
    return (template_state or "").strip().lower() == "paused"


def detect_trigger_now(template_state: str) -> bool:
    """Return True if the template is in the Trigger Now state."""
    return (template_state or "").strip().lower() == "trigger now"


def detect_error(template_state: str) -> bool:
    """Return True if the template is in the Error state.

    Error-state templates normally aren't fetched by the caller; this
    helper is defensive so the evaluator returns a terminal watermark
    decision rather than crashing if one slips through.
    """
    return (template_state or "").strip().lower() == "error"


# ---------------------------------------------------------------------------
# Cron + timezone parsing
# ---------------------------------------------------------------------------


def parse_cron_and_tz(cron_expr: str, tz_name: str):
    """Parse a cron expression + IANA timezone into a croniter iterator.

    Returns ``(croniter_instance, ZoneInfo)``. The croniter instance is
    anchored at Unix epoch UTC — callers re-anchor via ``set_current``
    (see ``compute_missed_slots``) so this function stays pure and
    cheap to call.

    Raises:
        CronParseError: cron_expr is not a valid croniter expression.
        TimezoneError: tz_name is not a valid IANA zone.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TimezoneError(f"invalid timezone {tz_name!r}: {exc}") from exc

    try:
        # Import lazily so that an environment without croniter can still
        # import this module for the canonicalize / detect helpers.
        from croniter import croniter  # type: ignore
    except ImportError as exc:  # pragma: no cover — dependency declared
        raise CronParseError(
            "croniter is required for schedule evaluation"
        ) from exc

    # Anchor at epoch — the concrete anchor is reset before each
    # compute_missed_slots call. Validate the expression here.
    try:
        itr = croniter(cron_expr, datetime(1970, 1, 1, tzinfo=tz))
    except Exception as exc:  # croniter raises CroniterError subclasses
        raise CronParseError(
            f"invalid cron expression {cron_expr!r}: {exc}"
        ) from exc
    return itr, tz


# ---------------------------------------------------------------------------
# Missed-slot computation
# ---------------------------------------------------------------------------


def compute_missed_slots(
    last_fired_slot: Optional[datetime],
    now: datetime,
    cron_expr: str,
    tz: ZoneInfo,
    *,
    earliest_anchor: Optional[datetime] = None,
    max_iterations: int = 10_000,
) -> list[datetime]:
    """Return cron slots strictly greater than ``last_fired_slot`` and <= ``now``.

    When ``last_fired_slot`` is None, the walk starts from
    ``earliest_anchor`` (typically the template's ``created_at``) so a
    brand-new template doesn't backfill years of slots. If
    ``earliest_anchor`` is also None we fall back to ``now`` minus one
    second (i.e. the next slot will be returned only if it already
    passed), which is the safe default for "no history available."

    Returns a list of tz-aware UTC datetimes at second precision.

    The explicit ``max_iterations`` guard is a defensive cap against
    pathological expressions. At 10 000 iterations a once-per-minute
    schedule caps out at ~7 days of backlog, far larger than any
    reasonable ``run_all_cap`` would consume.
    """
    from croniter import croniter  # type: ignore

    if last_fired_slot is not None:
        anchor = last_fired_slot
    elif earliest_anchor is not None:
        anchor = earliest_anchor
    else:
        anchor = now
    # Croniter needs a tz-aware anchor to produce tz-aware results.
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    anchor = anchor.astimezone(tz)

    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(
        tzinfo=timezone.utc
    )

    try:
        itr = croniter(cron_expr, anchor)
    except Exception as exc:
        raise CronParseError(
            f"invalid cron expression {cron_expr!r}: {exc}"
        ) from exc

    slots: list[datetime] = []
    for _ in range(max_iterations):
        next_dt = itr.get_next(datetime)
        # Normalize to tz-aware UTC at second precision.
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=tz)
        next_utc = next_dt.astimezone(timezone.utc).replace(microsecond=0)
        if next_utc > now_utc:
            break
        slots.append(next_utc)
    return slots


def next_fire_time(
    cron_expr: str,
    tz_name: str,
    now: datetime,
) -> datetime:
    """Return the next time the cron fires strictly after ``now``, in UTC.

    Pure helper used by the orchestrator to cache ``_template_next_fire_at``
    once per tick for the dashboard snapshot. Raises
    :class:`CronParseError` / :class:`TimezoneError` on invalid inputs —
    callers catch these to drive the R18 "move template to Error" flow.
    """
    _, tz = parse_cron_and_tz(cron_expr, tz_name)

    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(
        tzinfo=timezone.utc
    )
    anchor = now_utc.astimezone(tz)

    from croniter import croniter  # type: ignore

    try:
        itr = croniter(cron_expr, anchor)
    except Exception as exc:
        raise CronParseError(
            f"invalid cron expression {cron_expr!r}: {exc}"
        ) from exc

    next_dt = itr.get_next(datetime)
    if next_dt.tzinfo is None:
        next_dt = next_dt.replace(tzinfo=tz)
    return next_dt.astimezone(timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------------------
# on_missed policies
# ---------------------------------------------------------------------------


def apply_on_missed_policy(
    missed_slots: list[datetime],
    policy: str,
    cap: int,
) -> tuple[list[datetime], int]:
    """Apply the R9 on_missed policy to a list of missed slots.

    Returns ``(slots_to_fire, bounded_dropped_count)``.

    Policies:
        - ``skip``     : fire only the most recent slot, drop the rest
                         (no backfill). ``bounded_dropped_count`` is 0
                         because "skip" is policy-intended drop, not
                         bound exceeded.
        - ``run_once`` : fire the single most-recent missed slot.
        - ``run_all``  : fire up to ``cap`` oldest slots; anything beyond
                         counts as ``bounded_dropped_count``.
    """
    if not missed_slots:
        return [], 0

    policy = policy or "skip"
    if policy == "skip":
        # Fire nothing on catch-up; the next forward slot is handled
        # by the caller outside this function. The evaluator's
        # ``evaluate_template`` implements that: we treat "skip" as
        # "don't fire any missed slot".
        return [], 0
    if policy == "run_once":
        return [missed_slots[-1]], 0
    if policy == "run_all":
        if cap <= 0:
            return [], len(missed_slots)
        if len(missed_slots) <= cap:
            return list(missed_slots), 0
        fire = list(missed_slots[:cap])
        dropped = len(missed_slots) - cap
        return fire, dropped
    # Unknown policy — be loud rather than silently firing everything.
    raise ValueError(f"unknown on_missed policy: {policy!r}")


# ---------------------------------------------------------------------------
# Duplicate-label detection (R26)
# ---------------------------------------------------------------------------


def detect_duplicate_label(
    templates: Iterable[TemplateSnapshot],
    label_prefix: str = "schedule:",
) -> tuple[list[TemplateSnapshot], list[TemplateSnapshot]]:
    """Split templates into (winners, losers) by duplicate schedule label.

    Two templates carrying the same ``schedule:<type>`` label cannot both
    be valid — the dispatch path is keyed by schedule type. This helper
    resolves the conflict deterministically:

        winner = earliest ``created_at``, tiebroken by lexical
                 ``identifier`` ASC
        losers = everyone else with the same label

    Templates without any ``schedule:*`` label are silently ignored
    (they're not scheduled templates). A template carrying *multiple*
    ``schedule:*`` labels is itself malformed and is flagged as a loser
    in every conflict it's part of.

    The caller (orchestrator) is responsible for moving losers to the
    Error linear state.
    """
    # Group by label.
    by_label: dict[str, list[TemplateSnapshot]] = {}
    for tmpl in templates:
        schedule_labels = [
            lbl for lbl in tmpl.labels if lbl.startswith(label_prefix)
        ]
        if not schedule_labels:
            continue
        for lbl in schedule_labels:
            by_label.setdefault(lbl, []).append(tmpl)

    winners: list[TemplateSnapshot] = []
    losers: list[TemplateSnapshot] = []
    seen_winner_ids: set[str] = set()
    seen_loser_ids: set[str] = set()

    for lbl, group in by_label.items():
        if len(group) == 1:
            tmpl = group[0]
            if tmpl.id not in seen_winner_ids and tmpl.id not in seen_loser_ids:
                winners.append(tmpl)
                seen_winner_ids.add(tmpl.id)
            continue
        # Sort: created_at ASC (None pushed to end), then identifier ASC.
        ordered = sorted(
            group,
            key=lambda t: (
                t.created_at is None,
                t.created_at or datetime.max.replace(tzinfo=timezone.utc),
                t.identifier,
            ),
        )
        winner = ordered[0]
        if winner.id not in seen_loser_ids and winner.id not in seen_winner_ids:
            winners.append(winner)
            seen_winner_ids.add(winner.id)
        for loser in ordered[1:]:
            if loser.id in seen_winner_ids:
                # A template that wins one label but loses another is
                # ambiguous — promote it to loser across the board.
                # Remove from winners, add to losers.
                winners = [w for w in winners if w.id != loser.id]
                seen_winner_ids.discard(loser.id)
            if loser.id not in seen_loser_ids:
                losers.append(loser)
                seen_loser_ids.add(loser.id)

    return winners, losers


# ---------------------------------------------------------------------------
# Watermark introspection helpers
# ---------------------------------------------------------------------------


def _latest_watermark_by_slot(
    watermarks: Iterable[Watermark],
) -> dict[str, Watermark]:
    """Return {slot: latest_watermark}. Later seq / timestamp wins.

    Mirrors the oldest-first "last wins" semantics used by
    ``tracking.parse_latest_tracking``.
    """
    result: dict[str, Watermark] = {}
    for wm in watermarks:
        prev = result.get(wm.slot)
        if prev is None:
            result[wm.slot] = wm
            continue
        # seq first, then timestamp — seq is the authoritative ordering
        # per Unit 5 decisions; timestamp is a coarse fallback.
        prev_key = (prev.seq, prev.timestamp or datetime.min.replace(
            tzinfo=timezone.utc
        ))
        cur_key = (wm.seq, wm.timestamp or datetime.min.replace(
            tzinfo=timezone.utc
        ))
        if cur_key >= prev_key:
            result[wm.slot] = wm
    return result


def _last_fired_cron_slot(
    latest_by_slot: dict[str, Watermark],
) -> Optional[datetime]:
    """Return the most recent terminal cron slot (for use as the walk anchor).

    Trigger-Now slots (``trigger:`` prefix) are excluded — they don't
    advance the cron cursor. Skipped terminal watermarks (e.g.
    ``skipped_bounded``) DO advance it, otherwise ``run_all`` catch-up
    would re-propose the same dropped slots forever.
    """
    latest: Optional[datetime] = None
    for slot, wm in latest_by_slot.items():
        if slot.startswith("trigger:"):
            continue
        dt = _parse_canonical_slot(slot)
        if dt is None:
            continue
        if latest is None or dt > latest:
            latest = dt
    return latest


def _parse_canonical_slot(slot: str) -> Optional[datetime]:
    """Parse a canonical slot string back to UTC datetime. None on failure."""
    if slot.startswith("trigger:"):
        slot = slot[len("trigger:") :]
    try:
        # ``fromisoformat`` in 3.11+ handles the Z suffix as of 3.11,
        # but we stay defensive.
        iso = slot.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _slot_has_terminal_watermark(wm: Optional[Watermark]) -> bool:
    """True if the watermark is in a terminal state (no re-fire)."""
    if wm is None:
        return False
    return wm.status in {
        "child",
        "failed_permanent",
        "skipped_overlap",
        "skipped_bounded",
        "skipped_paused",
        "skipped_error",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def evaluate_template(
    template: TemplateSnapshot,
    recent_watermarks: list[Watermark],
    in_flight_children_count: int,
    now: datetime,
    schedule_cfg: "ScheduleConfig",
) -> list[FireDecision]:
    """Evaluate a single template and return zero-or-more fire decisions.

    The evaluator is intentionally multi-decision: a Trigger-Now fire and
    a coincident cron-slot fire can both be emitted in the same tick
    with distinct slot values.

    Ordering rules applied in this function:

    1. Error-state template → single ``skip_error`` decision. (Defensive;
       callers normally filter templates by linear state.)
    2. Paused template → single ``skip_paused`` decision.
    3. Trigger-Now state → emit one ``fire`` decision for
       ``trigger:<now>``. This bypasses overlap policy per the plan's
       Key Decision on Trigger-Now.
    4. Cron walk: missed-slot list ``apply_on_missed_policy`` →
       per-slot overlap-policy filter → ``fire`` decisions (or
       ``skip_overlap`` terminals).

    ``now`` should be UTC and tz-aware. Naive datetimes are interpreted
    as UTC (defensive).
    """
    # Normalize now.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    # State checks short-circuit before any cron math.
    if detect_error(template.linear_state):
        return [
            FireDecision(
                template_id=template.id,
                slot=canonicalize_slot(now),
                action="skip_error",
                reason="template_in_error_state",
            )
        ]
    if detect_paused(template.linear_state):
        return [
            FireDecision(
                template_id=template.id,
                slot=canonicalize_slot(now),
                action="skip_paused",
                reason="template_paused",
            )
        ]

    decisions: list[FireDecision] = []
    latest_by_slot = _latest_watermark_by_slot(recent_watermarks)

    # --- Trigger-Now ---------------------------------------------------
    if detect_trigger_now(template.linear_state):
        trigger_slot = _trigger_slot(now)
        prev = latest_by_slot.get(trigger_slot)
        # If by some coincidence we already fired this exact trigger
        # slot (same-second click storm), don't double-fire.
        if not _slot_has_terminal_watermark(prev):
            decisions.append(
                FireDecision(
                    template_id=template.id,
                    slot=trigger_slot,
                    action="fire",
                    reason="trigger_now",
                    is_trigger_now=True,
                )
            )

    # --- Cron walk -----------------------------------------------------
    last_fired = _last_fired_cron_slot(latest_by_slot)
    # Parse cron expression (may raise — caller handles Error transition).
    _, tz = parse_cron_and_tz(template.cron_expr, template.timezone)

    missed = compute_missed_slots(
        last_fired_slot=last_fired,
        now=now,
        cron_expr=template.cron_expr,
        tz=tz,
        earliest_anchor=template.created_at,
    )

    if not missed:
        return decisions

    policy = getattr(schedule_cfg, "on_missed", "skip") or "skip"
    cap = int(getattr(schedule_cfg, "run_all_cap", 5) or 5)

    if policy == "skip":
        # ``apply_on_missed_policy`` returns empty for skip — but the
        # intent of "skip" is: don't backfill, only honor the NEXT
        # upcoming slot that has already arrived. That means: fire
        # only the most recent missed slot (one decision), and
        # silently ignore the older ones — no watermarks for them.
        # This mirrors the test expectation: "5 missed slots → single
        # decision for next upcoming slot only."
        fire_slots = [missed[-1]]
        bounded_dropped = 0
        bounded_earliest = None
        bounded_latest = None
    else:
        fire_slots, bounded_dropped = apply_on_missed_policy(
            missed, policy, cap
        )
        bounded_earliest = (
            canonicalize_slot(missed[cap]) if bounded_dropped else None
        )
        bounded_latest = (
            canonicalize_slot(missed[-1]) if bounded_dropped else None
        )

    overlap_policy = getattr(schedule_cfg, "overlap_policy", "skip") or "skip"

    for slot_dt in fire_slots:
        slot_str = canonicalize_slot(slot_dt)
        prev = latest_by_slot.get(slot_str)
        if _slot_has_terminal_watermark(prev):
            # Already decided — never re-evaluate a terminal slot.
            continue
        # Overlap policy gate.
        if overlap_policy == "skip" and in_flight_children_count > 0:
            decisions.append(
                FireDecision(
                    template_id=template.id,
                    slot=slot_str,
                    action="skip_overlap",
                    reason="overlap_policy=skip,children_in_flight",
                )
            )
            continue
        # parallel / queue / cancel_previous → caller handles the
        # downstream mechanics (queueing / cancellation). The evaluator
        # just says "fire."
        decisions.append(
            FireDecision(
                template_id=template.id,
                slot=slot_str,
                action="fire",
                reason=None,
            )
        )

    # Emit the terminal bounded-drop decisions. One FireDecision per
    # dropped slot carrying the aggregate count on the *first* one keeps
    # the output compact while still giving the orchestrator what it
    # needs to post watermarks + the aggregate bounded_drop comment.
    if bounded_dropped:
        dropped_slots = missed[cap:]
        for idx, slot_dt in enumerate(dropped_slots):
            slot_str = canonicalize_slot(slot_dt)
            prev = latest_by_slot.get(slot_str)
            if _slot_has_terminal_watermark(prev):
                continue
            decisions.append(
                FireDecision(
                    template_id=template.id,
                    slot=slot_str,
                    action="skip_bounded",
                    reason="on_missed=run_all,cap_exceeded",
                    bounded_dropped_count=bounded_dropped if idx == 0 else 0,
                    bounded_drop_earliest=bounded_earliest if idx == 0 else None,
                    bounded_drop_latest=bounded_latest if idx == 0 else None,
                )
            )

    return decisions


__all__ = [
    "CronParseError",
    "TimezoneError",
    "TemplateSnapshot",
    "Watermark",
    "FireDecision",
    "canonicalize_slot",
    "detect_paused",
    "detect_trigger_now",
    "detect_error",
    "parse_cron_and_tz",
    "next_fire_time",
    "compute_missed_slots",
    "apply_on_missed_policy",
    "detect_duplicate_label",
    "evaluate_template",
]
