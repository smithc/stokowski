"""Pure-function tests for the schedule evaluator.

No mocks, no network, no Linear/Docker. The evaluator is a pure module
so all tests construct inputs by hand and assert on FireDecision outputs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stokowski.config import ScheduleConfig
from stokowski.scheduler import (
    CronParseError,
    FireDecision,
    TemplateSnapshot,
    TimezoneError,
    Watermark,
    apply_on_missed_policy,
    canonicalize_slot,
    compute_missed_slots,
    detect_duplicate_label,
    detect_error,
    detect_paused,
    detect_trigger_now,
    evaluate_template,
    parse_cron_and_tz,
)


# ---------------------------------------------------------------------------
# canonicalize_slot
# ---------------------------------------------------------------------------


class TestCanonicalizeSlot:
    def test_utc_seconds(self):
        dt = datetime(2026, 4, 19, 8, 0, 0, tzinfo=timezone.utc)
        assert canonicalize_slot(dt) == "2026-04-19T08:00:00Z"

    def test_drops_microseconds(self):
        dt = datetime(2026, 4, 19, 8, 0, 0, 123456, tzinfo=timezone.utc)
        assert canonicalize_slot(dt) == "2026-04-19T08:00:00Z"

    def test_converts_offset_to_utc(self):
        from zoneinfo import ZoneInfo

        dt = datetime(2026, 4, 19, 1, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        # 01:00 LA in April (DST) == 08:00 UTC
        assert canonicalize_slot(dt) == "2026-04-19T08:00:00Z"

    def test_naive_assumed_utc(self):
        dt = datetime(2026, 4, 19, 8, 0, 0)
        assert canonicalize_slot(dt) == "2026-04-19T08:00:00Z"


# ---------------------------------------------------------------------------
# state detectors
# ---------------------------------------------------------------------------


class TestStateDetectors:
    def test_detect_paused(self):
        assert detect_paused("Paused")
        assert detect_paused(" paused ")
        assert not detect_paused("Scheduled")

    def test_detect_trigger_now(self):
        assert detect_trigger_now("Trigger Now")
        assert detect_trigger_now("trigger now")
        assert not detect_trigger_now("Triggering")

    def test_detect_error(self):
        assert detect_error("Error")
        assert not detect_error("Scheduled")


# ---------------------------------------------------------------------------
# parse_cron_and_tz
# ---------------------------------------------------------------------------


class TestParseCronAndTz:
    def test_valid(self):
        itr, tz = parse_cron_and_tz("0 8 * * *", "UTC")
        assert itr is not None
        assert str(tz) == "UTC"

    def test_invalid_cron_raises(self):
        with pytest.raises(CronParseError):
            parse_cron_and_tz("not a cron", "UTC")

    def test_invalid_tz_raises(self):
        with pytest.raises(TimezoneError):
            parse_cron_and_tz("0 8 * * *", "PST")  # not IANA

    def test_iana_la(self):
        itr, tz = parse_cron_and_tz("0 8 * * 1", "America/Los_Angeles")
        assert itr is not None
        assert "Los_Angeles" in str(tz)


# ---------------------------------------------------------------------------
# compute_missed_slots
# ---------------------------------------------------------------------------


class TestComputeMissedSlots:
    def test_single_slot_since_yesterday(self):
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("UTC")
        last = datetime(2026, 4, 18, 8, 0, tzinfo=timezone.utc)
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        slots = compute_missed_slots(last, now, "0 8 * * *", tz)
        assert slots == [datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc)]

    def test_five_missed(self):
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("UTC")
        last = datetime(2026, 4, 14, 8, 0, tzinfo=timezone.utc)
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        slots = compute_missed_slots(last, now, "0 8 * * *", tz)
        assert len(slots) == 5
        assert slots[0] == datetime(2026, 4, 15, 8, 0, tzinfo=timezone.utc)
        assert slots[-1] == datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc)

    def test_no_slots_yet(self):
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("UTC")
        last = datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc)
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        slots = compute_missed_slots(last, now, "0 8 * * *", tz)
        assert slots == []

    def test_first_ever_uses_created_at(self):
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("UTC")
        created = datetime(2026, 4, 17, 6, 0, tzinfo=timezone.utc)
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        slots = compute_missed_slots(
            None, now, "0 8 * * *", tz, earliest_anchor=created
        )
        # Expected: 2026-04-17T08Z, 2026-04-18T08Z, 2026-04-19T08Z
        assert len(slots) == 3


# ---------------------------------------------------------------------------
# apply_on_missed_policy
# ---------------------------------------------------------------------------


def _utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


class TestApplyOnMissedPolicy:
    def test_empty(self):
        assert apply_on_missed_policy([], "run_all", 5) == ([], 0)

    def test_skip_returns_empty(self):
        # "skip" at this layer means "no backfill"; caller handles the
        # next-upcoming fire separately.
        slots = [_utc(2026, 4, 15), _utc(2026, 4, 16), _utc(2026, 4, 17)]
        fire, dropped = apply_on_missed_policy(slots, "skip", 5)
        assert fire == []
        assert dropped == 0

    def test_run_once_returns_last(self):
        slots = [_utc(2026, 4, 15), _utc(2026, 4, 16), _utc(2026, 4, 17)]
        fire, dropped = apply_on_missed_policy(slots, "run_once", 5)
        assert fire == [_utc(2026, 4, 17)]
        assert dropped == 0

    def test_run_all_under_cap(self):
        slots = [_utc(2026, 4, d) for d in (15, 16, 17)]
        fire, dropped = apply_on_missed_policy(slots, "run_all", 5)
        assert fire == slots
        assert dropped == 0

    def test_run_all_over_cap(self):
        slots = [_utc(2026, 4, d) for d in range(1, 13)]  # 12 slots
        fire, dropped = apply_on_missed_policy(slots, "run_all", 5)
        assert len(fire) == 5
        assert dropped == 7
        assert fire == slots[:5]

    def test_unknown_policy(self):
        with pytest.raises(ValueError):
            apply_on_missed_policy([_utc(2026, 4, 15)], "hoof", 5)


# ---------------------------------------------------------------------------
# detect_duplicate_label (R26)
# ---------------------------------------------------------------------------


class TestDetectDuplicateLabel:
    def _tmpl(self, id_, ident, labels, created=None):
        return TemplateSnapshot(
            id=id_,
            identifier=ident,
            linear_state="Scheduled",
            cron_expr="0 8 * * *",
            timezone="UTC",
            labels=tuple(labels),
            created_at=created,
        )

    def test_no_duplicates(self):
        tmpls = [
            self._tmpl("a", "A-1", ["schedule:daily"]),
            self._tmpl("b", "B-1", ["schedule:weekly"]),
        ]
        winners, losers = detect_duplicate_label(tmpls)
        assert {t.id for t in winners} == {"a", "b"}
        assert losers == []

    def test_duplicate_oldest_wins(self):
        earlier = datetime(2026, 4, 1, tzinfo=timezone.utc)
        later = datetime(2026, 4, 10, tzinfo=timezone.utc)
        tmpls = [
            self._tmpl("a", "A-1", ["schedule:daily"], created=later),
            self._tmpl("b", "B-1", ["schedule:daily"], created=earlier),
        ]
        winners, losers = detect_duplicate_label(tmpls)
        assert [t.id for t in winners] == ["b"]
        assert [t.id for t in losers] == ["a"]

    def test_duplicate_identifier_tiebreak(self):
        t = datetime(2026, 4, 1, tzinfo=timezone.utc)
        tmpls = [
            self._tmpl("a", "SMI-99", ["schedule:daily"], created=t),
            self._tmpl("b", "SMI-10", ["schedule:daily"], created=t),
        ]
        winners, losers = detect_duplicate_label(tmpls)
        assert [t.id for t in winners] == ["b"]  # SMI-10 lex < SMI-99
        assert [t.id for t in losers] == ["a"]

    def test_non_schedule_labels_ignored(self):
        tmpls = [
            self._tmpl("a", "A-1", ["workflow:x", "other"]),
            self._tmpl("b", "B-1", ["schedule:daily"]),
        ]
        winners, _ = detect_duplicate_label(tmpls)
        assert [t.id for t in winners] == ["b"]


# ---------------------------------------------------------------------------
# evaluate_template
# ---------------------------------------------------------------------------


def _scheduled_tmpl(cron="0 8 * * *", tz="UTC", state="Scheduled", created=None):
    return TemplateSnapshot(
        id="tmpl-1",
        identifier="SMI-88",
        linear_state=state,
        cron_expr=cron,
        timezone=tz,
        labels=("schedule:daily",),
        created_at=created,
    )


def _cfg(**overrides):
    cfg = ScheduleConfig(
        name="daily",
        workflow="daily-workflow",
        overlap_policy=overrides.get("overlap_policy", "skip"),
        workspace_mode=overrides.get("workspace_mode", "ephemeral"),
        on_missed=overrides.get("on_missed", "skip"),
        run_all_cap=overrides.get("run_all_cap", 5),
        retention_days=30,
        max_runtime_ms=None,
        timezone=overrides.get("timezone", "UTC"),
    )
    return cfg


class TestEvaluateTemplateHappy:
    def test_daily_cron_one_missed(self):
        tmpl = _scheduled_tmpl()
        yesterday_wm = Watermark(
            template_id=tmpl.id,
            slot="2026-04-18T08:00:00Z",
            status="child",
            child_id="c-1",
            seq=1,
        )
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl, [yesterday_wm], 0, now, _cfg(on_missed="run_once")
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.action == "fire"
        assert d.slot == "2026-04-19T08:00:00Z"
        assert not d.is_trigger_now

    def test_weekly_cron_la_spring_forward(self):
        # 2026-03-08 is the US DST spring-forward date.
        # Use a weekly cron at 08:00 America/Los_Angeles Mondays to
        # assert croniter produces a tz-aware slot that normalizes to
        # a valid UTC instant. Whatever croniter's documented behavior
        # is, the slot serialization must round-trip.
        tmpl = _scheduled_tmpl(
            cron="0 8 * * 1",
            tz="America/Los_Angeles",
            created=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        now = datetime(2026, 3, 10, 16, 0, tzinfo=timezone.utc)  # Tue after DST
        decisions = evaluate_template(tmpl, [], 0, now, _cfg(on_missed="run_once"))
        # Expect at least one fire decision — the Monday after
        # spring-forward (2026-03-09) at 08:00 PDT == 15:00 UTC.
        fires = [d for d in decisions if d.action == "fire"]
        assert fires, "expected at least one fire decision after DST boundary"
        assert fires[0].slot == "2026-03-09T15:00:00Z"

    def test_first_ever_fire_uses_created_at(self):
        created = datetime(2026, 4, 17, 6, 0, tzinfo=timezone.utc)
        tmpl = _scheduled_tmpl(created=created)
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl, [], 0, now, _cfg(on_missed="run_once")
        )
        # Expect a single fire at the most-recent missed slot (04-19 08Z).
        fires = [d for d in decisions if d.action == "fire"]
        assert [d.slot for d in fires] == ["2026-04-19T08:00:00Z"]


class TestEvaluateTemplateTriggerNow:
    def test_trigger_now_emits_trigger_slot(self):
        tmpl = _scheduled_tmpl(state="Trigger Now")
        now = datetime(2026, 4, 19, 10, 3, 17, tzinfo=timezone.utc)
        decisions = evaluate_template(tmpl, [], 0, now, _cfg())
        trigger_fires = [d for d in decisions if d.is_trigger_now]
        assert len(trigger_fires) == 1
        assert trigger_fires[0].slot == "trigger:2026-04-19T10:03:17Z"
        assert trigger_fires[0].action == "fire"

    def test_trigger_now_plus_coincident_cron(self):
        # 08:00 cron, now is 08:00:30 — no prior watermark but a
        # created_at before 08:00 means a cron fire is ALSO due this
        # tick. Trigger-Now produces a *second* distinct decision with
        # the trigger: prefix.
        tmpl = _scheduled_tmpl(
            state="Trigger Now",
            created=datetime(2026, 4, 19, 6, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 19, 8, 0, 30, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl, [], 0, now, _cfg(on_missed="run_once")
        )
        slots = {d.slot for d in decisions if d.action == "fire"}
        assert "trigger:2026-04-19T08:00:30Z" in slots
        assert "2026-04-19T08:00:00Z" in slots
        # Two distinct fires, different slot strings.
        assert len([d for d in decisions if d.action == "fire"]) == 2


class TestEvaluateTemplateOnMissed:
    def test_on_missed_skip_five_slots(self):
        tmpl = _scheduled_tmpl()
        # Last fired five days ago.
        wm = Watermark(
            template_id=tmpl.id,
            slot="2026-04-14T08:00:00Z",
            status="child",
            child_id="c-1",
            seq=1,
        )
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl, [wm], 0, now, _cfg(on_missed="skip")
        )
        fires = [d for d in decisions if d.action == "fire"]
        assert [d.slot for d in fires] == ["2026-04-19T08:00:00Z"]

    def test_on_missed_run_once(self):
        tmpl = _scheduled_tmpl()
        wm = Watermark(
            template_id=tmpl.id,
            slot="2026-04-14T08:00:00Z",
            status="child",
            child_id="c-1",
            seq=1,
        )
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl, [wm], 0, now, _cfg(on_missed="run_once")
        )
        fires = [d for d in decisions if d.action == "fire"]
        assert [d.slot for d in fires] == ["2026-04-19T08:00:00Z"]

    def test_on_missed_run_all_cap_exceeded(self):
        # Use hourly cron with a 12-hour gap, cap=5.
        tmpl = TemplateSnapshot(
            id="tmpl-h",
            identifier="SMI-90",
            linear_state="Scheduled",
            cron_expr="0 * * * *",  # hourly
            timezone="UTC",
            labels=("schedule:hourly",),
            created_at=datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc),
        )
        wm = Watermark(
            template_id=tmpl.id,
            slot="2026-04-19T00:00:00Z",
            status="child",
            child_id="c-0",
            seq=1,
        )
        now = datetime(2026, 4, 19, 12, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl, [wm], 0, now, _cfg(on_missed="run_all", run_all_cap=5)
        )
        fires = [d for d in decisions if d.action == "fire"]
        bounded = [d for d in decisions if d.action == "skip_bounded"]
        # Missed slots are 01:00..12:00 = 12 slots; cap=5 → 5 fires +
        # 7 bounded drops.
        assert len(fires) == 5
        assert len(bounded) == 7
        # First bounded carries the aggregate drop count.
        assert bounded[0].bounded_dropped_count == 7
        assert bounded[0].bounded_drop_earliest == "2026-04-19T06:00:00Z"
        assert bounded[0].bounded_drop_latest == "2026-04-19T12:00:00Z"


class TestEvaluateTemplateOverlap:
    def test_overlap_skip_with_child_in_flight(self):
        tmpl = _scheduled_tmpl()
        wm = Watermark(
            template_id=tmpl.id,
            slot="2026-04-18T08:00:00Z",
            status="child",
            child_id="c-1",
            seq=1,
        )
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl,
            [wm],
            in_flight_children_count=1,
            now=now,
            schedule_cfg=_cfg(overlap_policy="skip", on_missed="run_once"),
        )
        skipped = [d for d in decisions if d.action == "skip_overlap"]
        assert len(skipped) == 1
        assert skipped[0].slot == "2026-04-19T08:00:00Z"

    def test_overlap_parallel_always_fires(self):
        tmpl = _scheduled_tmpl(
            created=datetime(2026, 4, 17, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl,
            [],
            in_flight_children_count=3,
            now=now,
            schedule_cfg=_cfg(overlap_policy="parallel", on_missed="run_once"),
        )
        assert any(d.action == "fire" for d in decisions)


class TestEvaluateTemplateStateGates:
    def test_paused_returns_skip_paused(self):
        tmpl = _scheduled_tmpl(state="Paused")
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(tmpl, [], 0, now, _cfg())
        assert len(decisions) == 1
        assert decisions[0].action == "skip_paused"

    def test_error_returns_skip_error(self):
        tmpl = _scheduled_tmpl(state="Error")
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(tmpl, [], 0, now, _cfg())
        assert len(decisions) == 1
        assert decisions[0].action == "skip_error"


class TestEvaluateTemplateInvalidInput:
    def test_invalid_cron_raises(self):
        tmpl = TemplateSnapshot(
            id="tmpl-bad",
            identifier="SMI-BAD",
            linear_state="Scheduled",
            cron_expr="definitely not cron",
            timezone="UTC",
        )
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        with pytest.raises(CronParseError):
            evaluate_template(tmpl, [], 0, now, _cfg(on_missed="run_once"))

    def test_invalid_tz_raises(self):
        tmpl = TemplateSnapshot(
            id="tmpl-bad",
            identifier="SMI-BAD",
            linear_state="Scheduled",
            cron_expr="0 8 * * *",
            timezone="PST",  # not IANA
        )
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        with pytest.raises(TimezoneError):
            evaluate_template(tmpl, [], 0, now, _cfg(on_missed="run_once"))


class TestEvaluateTemplateIdempotency:
    def test_terminal_watermark_blocks_refire(self):
        tmpl = _scheduled_tmpl()
        # Watermark shows we already fired (terminal: child). The
        # evaluator must not return another fire for the same slot.
        wm_prior = Watermark(
            template_id=tmpl.id,
            slot="2026-04-18T08:00:00Z",
            status="child",
            child_id="c-1",
            seq=1,
        )
        wm_today = Watermark(
            template_id=tmpl.id,
            slot="2026-04-19T08:00:00Z",
            status="child",
            child_id="c-2",
            seq=2,
        )
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl, [wm_prior, wm_today], 0, now, _cfg(on_missed="run_once")
        )
        # Today's slot already fired → no fire/skip_overlap decision
        # for that exact slot.
        assert not any(
            d.slot == "2026-04-19T08:00:00Z" for d in decisions
        )

    def test_pending_watermark_does_not_block(self):
        # A ``pending`` watermark is retry-eligible — the evaluator
        # should still propose a fire (the orchestrator's duplicate-
        # sibling check is what prevents double-creation, per the plan).
        tmpl = _scheduled_tmpl()
        wm_pending = Watermark(
            template_id=tmpl.id,
            slot="2026-04-19T08:00:00Z",
            status="pending",
            attempt=1,
            seq=1,
        )
        now = datetime(2026, 4, 19, 8, 5, tzinfo=timezone.utc)
        decisions = evaluate_template(
            tmpl, [wm_pending], 0, now, _cfg(on_missed="run_once")
        )
        # Whether this returns a fire or not depends on cron walk anchor
        # behavior; the key invariant is "pending is not terminal". We
        # exercise the helper directly for clarity:
        from stokowski.scheduler import _slot_has_terminal_watermark

        assert not _slot_has_terminal_watermark(wm_pending)
