"""Pure helpers for the fire-materialization path (Unit 6).

Extracted from ``Orchestrator._materialize_fire`` so the load-bearing
decisions (duplicate-sibling detection, label composition, child copy
rendering, watermark → ``Watermark`` adaptation) are trivially unit-testable
without instantiating an orchestrator.

Everything in this module is I/O-free and mutation-free, matching the
``stokowski.scheduler`` discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal, Optional

from .models import Issue
from .scheduler import FireDecision, Watermark

# Hard cap on fire-attempt retries before the template is moved to Error.
# Matches the plan default (Unit 6). Exposed as a module constant so tests
# can reference the same symbol instead of hard-coding.
MAX_FIRE_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Label / copy builders
# ---------------------------------------------------------------------------


def slot_label_name(canonical_slot: str) -> str:
    """Return the Linear label name used as the duplicate-detection primitive.

    We try the colon form first because it reads naturally in Linear's UI.
    Operators whose Linear workspace rejects colons in label names can
    migrate to ``slot__<slot>`` later; the materializer uses whatever this
    helper returns so swap logic lives in one place.

    The slot is already canonicalized (see ``scheduler.canonicalize_slot``)
    so the returned label name is deterministic for a given UTC second.
    """
    return f"slot:{canonical_slot}"


def workflow_label_name(schedule_name: str) -> str:
    """Return the ``schedule:<name>`` label the template itself carries.

    Children inherit this label so operators can filter on "all fires of
    schedule X" in a single Linear view.
    """
    return f"schedule:{schedule_name}"


def child_has_slot_label(
    child: Issue, canonical_slot: str
) -> bool:
    """True if ``child`` carries the slot label for ``canonical_slot``.

    Linear label lookups in this codebase are case-insensitive (see
    ``_normalize_issue``), so compare lowercased strings. The canonical
    slot is already ASCII-safe.
    """
    target = slot_label_name(canonical_slot).lower()
    for label in (child.labels or []):
        if (label or "").lower() == target:
            return True
    return False


def find_existing_child_for_slot(
    children: Iterable[Issue], canonical_slot: str
) -> Optional[Issue]:
    """Return the first non-archived active child carrying the slot label.

    Used by step 1 of ``_materialize_fire`` to recover from
    crash-between-create-and-watermark-update. Callers filter by
    "active" vs. "archived" upstream where possible; we also honor
    ``archived_at`` here defensively so a stale archived sibling never
    shadows a fresh fire.
    """
    for child in children:
        if child.archived_at is not None:
            continue
        if child_has_slot_label(child, canonical_slot):
            return child
    return None


def build_child_title(template_title: str, slot: str) -> str:
    """Render the child's title per the plan's convention.

    The agent is free to edit the title once the child exists; this is
    only the *initial* title. We keep the slot suffix readable for humans
    reviewing the sibling list.
    """
    title = (template_title or "").strip() or "Scheduled fire"
    return f"{title} — fire {slot}"


def build_child_description(
    *,
    template_identifier: str,
    template_url: Optional[str],
    slot: str,
    cron_expr: Optional[str],
    schedule_name: Optional[str],
    is_trigger: bool,
) -> str:
    """Render the child's description (audit trail + routing note).

    Kept deliberately compact so Linear timeline rendering stays readable.
    Fields use markdown bullets instead of a table — Linear's GraphQL
    description input does render tables but we avoid them for agent-read
    simplicity.
    """
    lines: list[str] = []
    header = "Triggered fire" if is_trigger else "Scheduled fire"
    lines.append(f"**{header}** of `{template_identifier}`.")
    lines.append("")
    if template_url:
        lines.append(f"- Parent: [{template_identifier}]({template_url})")
    else:
        lines.append(f"- Parent: {template_identifier}")
    lines.append(f"- Slot: `{slot}`")
    if cron_expr and not is_trigger:
        lines.append(f"- Cron: `{cron_expr}`")
    if schedule_name:
        lines.append(f"- Schedule: `{schedule_name}`")
    lines.append("")
    lines.append(
        "_This issue was materialized by Stokowski and will be dispatched "
        "to an agent on the next poll tick. Do not edit the slot label — "
        "it is the duplicate-detection primitive._"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Watermark adapter
# ---------------------------------------------------------------------------


def watermarks_from_parsed(
    parsed: dict[str, dict[str, Any]], template_id: str
) -> list[Watermark]:
    """Convert the ``parse_fired_by_slot`` dict-form into ``Watermark``s.

    The tracking module returns raw payloads for forward-compat reasons;
    the evaluator consumes typed ``Watermark`` records. This adapter
    silently drops malformed entries rather than raising — evaluator
    input is best-effort by design.
    """
    out: list[Watermark] = []
    for slot, payload in parsed.items():
        try:
            ts_raw = payload.get("timestamp")
            ts_dt: Optional[datetime] = None
            if isinstance(ts_raw, str) and ts_raw:
                try:
                    ts_dt = datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00")
                    )
                except ValueError:
                    ts_dt = None
            seq_raw = payload.get("seq")
            try:
                seq_val = int(seq_raw) if seq_raw is not None else 0
            except (TypeError, ValueError):
                seq_val = 0
            attempt_raw = payload.get("attempt")
            try:
                attempt_val = (
                    int(attempt_raw) if attempt_raw is not None else None
                )
            except (TypeError, ValueError):
                attempt_val = None
            out.append(
                Watermark(
                    template_id=template_id,
                    slot=slot,
                    status=str(payload.get("status") or "").strip(),
                    child_id=(
                        payload.get("child")
                        if isinstance(payload.get("child"), str)
                        else None
                    ),
                    attempt=attempt_val,
                    reason=(
                        payload.get("reason")
                        if isinstance(payload.get("reason"), str)
                        else None
                    ),
                    timestamp=ts_dt,
                    seq=seq_val,
                )
            )
        except Exception:
            # Forward-compat: malformed entries are ignored rather than
            # aborting the whole evaluator pass.
            continue
    return out


def max_seq_from_parsed(parsed: dict[str, dict[str, Any]]) -> int:
    """Return the maximum ``seq`` value across all parsed watermarks.

    Used for one-shot startup seeding of ``_template_watermark_seq``
    (so restart doesn't reset seq to 0 and create a brief window where
    new watermarks tie with old ones). Returns 0 when no parseable
    ``seq`` fields exist.
    """
    best = 0
    for payload in parsed.values():
        seq = payload.get("seq")
        try:
            if seq is None:
                continue
            n = int(seq)
            if n > best:
                best = n
        except (TypeError, ValueError):
            continue
    return best


# ---------------------------------------------------------------------------
# Action routing (pure)
# ---------------------------------------------------------------------------


FireAction = Literal[
    "fire",
    "skip_overlap",
    "skip_bounded",
    "skip_paused",
    "skip_error",
]


@dataclass(frozen=True)
class MaterializeStep:
    """Pure description of what ``_materialize_fire`` will do for a decision.

    The orchestrator method itself performs the I/O; this record captures
    the *decision* it will make given its inputs, which lets tests assert
    on the ordering without a live Linear.
    """

    kind: Literal[
        "duplicate",       # existing sibling found → promote pending → child
        "post_pending",    # no sibling → post pending watermark
        "fail_permanent",  # attempts exceeded → skip to Error transition
    ]
    existing_child_id: Optional[str] = None
    attempt_number: int = 0


def decide_materialize_step(
    decision: FireDecision,
    existing_children: list[Issue],
    current_attempts: int,
    max_attempts: int = MAX_FIRE_ATTEMPTS,
) -> MaterializeStep:
    """Pick the first concrete step ``_materialize_fire`` should perform.

    Rules:
    1. If a sibling child already carries the slot label → ``duplicate``.
    2. Else if attempts have already exhausted the retry budget →
       ``fail_permanent`` (fast path — don't post another pending just
       to watch it fail again).
    3. Else → ``post_pending`` with the next attempt number.

    ``current_attempts`` is the pre-increment counter: 0 on first try,
    1 after one failure, etc.
    """
    if decision.action != "fire":  # defensive — caller should have routed
        return MaterializeStep(
            kind="post_pending", attempt_number=current_attempts + 1
        )

    sibling = find_existing_child_for_slot(existing_children, decision.slot)
    if sibling is not None:
        return MaterializeStep(
            kind="duplicate", existing_child_id=sibling.id
        )

    if current_attempts >= max_attempts:
        return MaterializeStep(
            kind="fail_permanent", attempt_number=current_attempts
        )

    return MaterializeStep(
        kind="post_pending", attempt_number=current_attempts + 1
    )


__all__ = [
    "MAX_FIRE_ATTEMPTS",
    "MaterializeStep",
    "build_child_description",
    "build_child_title",
    "child_has_slot_label",
    "decide_materialize_step",
    "find_existing_child_for_slot",
    "max_seq_from_parsed",
    "slot_label_name",
    "watermarks_from_parsed",
    "workflow_label_name",
]
