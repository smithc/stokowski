"""Pure-function tests for scheduled-job watermark tracking primitives.

No mocks, no network, no Linear/Docker. Covers:
- stokowski:fired comment construction + regex round-trip
- parse_latest_fired / parse_fired_by_slot supersession semantics
- bounded-drop and schedule-error comment formats
- get_comments_since filter continues to exclude watermark comments
"""

from __future__ import annotations

import json

from stokowski.tracking import (
    BOUNDED_DROP_PATTERN,
    FIRED_PATTERN,
    SCHEDULE_ERROR_PATTERN,
    get_comments_since,
    make_bounded_drop_comment,
    make_fired_comment,
    make_schedule_error_comment,
    parse_fired_by_slot,
    parse_latest_fired,
    parse_latest_schedule_error,
)


def _comment(body: str, created_at: str = "2026-04-19T12:00:00+00:00") -> dict:
    """Build the minimal Linear-comment-shaped dict the tracking module consumes."""
    return {"body": body, "createdAt": created_at}


# ---------------------------------------------------------------------------
# make_fired_comment / FIRED_PATTERN round-trip
# ---------------------------------------------------------------------------


class TestMakeFiredComment:
    def test_regex_roundtrip_minimal(self):
        body = make_fired_comment(
            template_id="T-1",
            slot="2026-04-19T08:00:00+00:00",
            status="pending",
        )
        match = FIRED_PATTERN.search(body)
        assert match is not None
        payload = json.loads(match.group(1))
        assert payload["template"] == "T-1"
        assert payload["slot"] == "2026-04-19T08:00:00+00:00"
        assert payload["status"] == "pending"
        assert "timestamp" in payload

    def test_regex_roundtrip_all_fields(self):
        body = make_fired_comment(
            template_id="T-1",
            slot="2026-04-19T08:00:00+00:00",
            status="child",
            child_id="SMI-201",
            attempt=2,
            reason=None,
            seq=7,
            timestamp="2026-04-19T08:00:01+00:00",
        )
        payload = json.loads(FIRED_PATTERN.search(body).group(1))
        assert payload == {
            "template": "T-1",
            "slot": "2026-04-19T08:00:00+00:00",
            "status": "child",
            "timestamp": "2026-04-19T08:00:01+00:00",
            "child": "SMI-201",
            "attempt": 2,
            "seq": 7,
        }

    def test_human_line_for_child_status(self):
        body = make_fired_comment(
            template_id="T-1",
            slot="2026-04-19T08:00:00+00:00",
            status="child",
            child_id="SMI-201",
        )
        assert "SMI-201" in body
        assert "2026-04-19T08:00:00+00:00" in body

    def test_skipped_status_human_line(self):
        body = make_fired_comment(
            template_id="T-1",
            slot="2026-04-19T08:00:00+00:00",
            status="skipped_bounded",
            reason="cap_exceeded",
        )
        assert "skipped" in body
        assert "bounded" in body


# ---------------------------------------------------------------------------
# parse_latest_fired
# ---------------------------------------------------------------------------


class TestParseLatestFired:
    def test_empty_returns_none(self):
        assert parse_latest_fired([]) is None

    def test_no_fired_comments_returns_none(self):
        comments = [
            _comment("just a plain comment"),
            _comment("<!-- stokowski:state {\"state\":\"review\"} -->"),
        ]
        assert parse_latest_fired(comments) is None

    def test_last_wins_across_slots(self):
        first = make_fired_comment(
            template_id="T-1",
            slot="2026-04-19T08:00:00+00:00",
            status="child",
            child_id="SMI-201",
            seq=1,
            timestamp="2026-04-19T08:00:05+00:00",
        )
        second = make_fired_comment(
            template_id="T-1",
            slot="2026-04-19T09:00:00+00:00",
            status="pending",
            seq=2,
            timestamp="2026-04-19T09:00:01+00:00",
        )
        latest = parse_latest_fired([_comment(first), _comment(second)])
        assert latest is not None
        assert latest["slot"] == "2026-04-19T09:00:00+00:00"
        assert latest["status"] == "pending"

    def test_forward_compat_unknown_fields_ignored(self):
        # Payload with an unknown "future_field" — parser must still return it.
        raw = (
            '<!-- stokowski:fired {"template":"T-1","slot":"s","status":"child",'
            '"timestamp":"2026-04-19T08:00:00+00:00","future_field":42} -->'
        )
        latest = parse_latest_fired([_comment(raw)])
        assert latest is not None
        assert latest["future_field"] == 42
        assert latest["status"] == "child"

    def test_malformed_json_skipped(self):
        good = make_fired_comment(
            template_id="T-1",
            slot="s",
            status="child",
            child_id="SMI-99",
        )
        bad = "<!-- stokowski:fired {not valid json} -->"
        latest = parse_latest_fired([_comment(bad), _comment(good)])
        assert latest is not None
        assert latest["status"] == "child"
        # Ensure bad comment doesn't crash even when it comes last.
        latest2 = parse_latest_fired([_comment(good), _comment(bad)])
        assert latest2 is not None
        assert latest2["status"] == "child"


# ---------------------------------------------------------------------------
# parse_fired_by_slot
# ---------------------------------------------------------------------------


class TestParseFiredBySlot:
    def test_empty_returns_empty_dict(self):
        assert parse_fired_by_slot([]) == {}

    def test_three_slots_each_latest_kept(self):
        c1 = make_fired_comment(
            template_id="T-1",
            slot="slot-A",
            status="child",
            child_id="SMI-1",
            seq=1,
            timestamp="2026-04-19T08:00:00+00:00",
        )
        c2 = make_fired_comment(
            template_id="T-1",
            slot="slot-B",
            status="child",
            child_id="SMI-2",
            seq=2,
            timestamp="2026-04-19T08:05:00+00:00",
        )
        c3 = make_fired_comment(
            template_id="T-1",
            slot="slot-C",
            status="pending",
            seq=3,
            timestamp="2026-04-19T08:10:00+00:00",
        )
        result = parse_fired_by_slot(
            [_comment(c1), _comment(c2), _comment(c3)]
        )
        assert set(result.keys()) == {"slot-A", "slot-B", "slot-C"}
        assert result["slot-A"]["child"] == "SMI-1"
        assert result["slot-B"]["child"] == "SMI-2"
        assert result["slot-C"]["status"] == "pending"

    def test_supersession_pending_then_child(self):
        pending = make_fired_comment(
            template_id="T-1",
            slot="2026-04-19T08:00:00+00:00",
            status="pending",
            attempt=1,
            seq=1,
            timestamp="2026-04-19T08:00:01+00:00",
        )
        child = make_fired_comment(
            template_id="T-1",
            slot="2026-04-19T08:00:00+00:00",
            status="child",
            child_id="SMI-201",
            seq=2,
            timestamp="2026-04-19T08:00:02+00:00",
        )
        result = parse_fired_by_slot([_comment(pending), _comment(child)])
        assert len(result) == 1
        assert result["2026-04-19T08:00:00+00:00"]["status"] == "child"
        assert result["2026-04-19T08:00:00+00:00"]["child"] == "SMI-201"

    def test_seq_tiebreak_with_identical_timestamps(self):
        # Same timestamp, different seq — higher seq must win.
        shared_ts = "2026-04-19T08:00:00+00:00"
        low = make_fired_comment(
            template_id="T-1",
            slot="slot-X",
            status="pending",
            seq=3,
            timestamp=shared_ts,
        )
        high = make_fired_comment(
            template_id="T-1",
            slot="slot-X",
            status="child",
            child_id="SMI-500",
            seq=10,
            timestamp=shared_ts,
        )
        # Order doesn't matter — seq is the tiebreak.
        result = parse_fired_by_slot([_comment(high), _comment(low)])
        assert result["slot-X"]["status"] == "child"
        assert result["slot-X"]["seq"] == 10
        result2 = parse_fired_by_slot([_comment(low), _comment(high)])
        assert result2["slot-X"]["status"] == "child"
        assert result2["slot-X"]["seq"] == 10

    def test_forward_compat_unknown_fields(self):
        raw = (
            '<!-- stokowski:fired {"template":"T-1","slot":"s1","status":"child",'
            '"timestamp":"2026-04-19T08:00:00+00:00","mystery":["a",1]} -->'
        )
        result = parse_fired_by_slot([_comment(raw)])
        assert result["s1"]["mystery"] == ["a", 1]

    def test_malformed_json_skipped(self):
        good = make_fired_comment(
            template_id="T-1",
            slot="good-slot",
            status="child",
            child_id="SMI-9",
        )
        bad = "<!-- stokowski:fired {this isn't json} -->"
        result = parse_fired_by_slot([_comment(bad), _comment(good)])
        assert "good-slot" in result
        assert result["good-slot"]["status"] == "child"

    def test_watermark_without_slot_ignored(self):
        raw = (
            '<!-- stokowski:fired {"template":"T-1","status":"child",'
            '"timestamp":"2026-04-19T08:00:00+00:00"} -->'
        )
        assert parse_fired_by_slot([_comment(raw)]) == {}


# ---------------------------------------------------------------------------
# Bounded-drop comment
# ---------------------------------------------------------------------------


class TestBoundedDropComment:
    def test_regex_roundtrip(self):
        body = make_bounded_drop_comment(
            template_id="T-1",
            dropped_count=7,
            earliest_slot="2026-04-19T06:00:00+00:00",
            latest_slot="2026-04-19T08:00:00+00:00",
        )
        match = BOUNDED_DROP_PATTERN.search(body)
        assert match is not None
        payload = json.loads(match.group(1))
        assert payload["template"] == "T-1"
        assert payload["dropped_count"] == 7
        assert payload["earliest_slot"] == "2026-04-19T06:00:00+00:00"
        assert payload["latest_slot"] == "2026-04-19T08:00:00+00:00"
        assert "timestamp" in payload

    def test_human_line_present(self):
        body = make_bounded_drop_comment(
            template_id="T-1",
            dropped_count=3,
            earliest_slot="s-early",
            latest_slot="s-late",
        )
        assert "3" in body
        assert "s-early" in body
        assert "s-late" in body


# ---------------------------------------------------------------------------
# Schedule-error comment + parser
# ---------------------------------------------------------------------------


class TestScheduleErrorComment:
    def test_regex_roundtrip(self):
        body = make_schedule_error_comment(
            template_id="T-1",
            reason="invalid_cron",
            details="cron expression has 7 fields",
        )
        match = SCHEDULE_ERROR_PATTERN.search(body)
        assert match is not None
        payload = json.loads(match.group(1))
        assert payload["template"] == "T-1"
        assert payload["reason"] == "invalid_cron"
        assert payload["details"] == "cron expression has 7 fields"
        assert "timestamp" in payload

    def test_parse_latest_empty(self):
        assert parse_latest_schedule_error([]) is None

    def test_parse_latest_wins(self):
        first = make_schedule_error_comment(
            template_id="T-1",
            reason="invalid_cron",
            timestamp="2026-04-19T08:00:00+00:00",
        )
        second = make_schedule_error_comment(
            template_id="T-1",
            reason="invalid_timezone",
            details="unknown TZ: Foo/Bar",
            timestamp="2026-04-19T09:00:00+00:00",
        )
        latest = parse_latest_schedule_error(
            [_comment(first), _comment(second)]
        )
        assert latest is not None
        assert latest["reason"] == "invalid_timezone"
        assert latest["details"] == "unknown TZ: Foo/Bar"

    def test_parse_latest_malformed_skipped(self):
        bad = "<!-- stokowski:schedule_error {nope} -->"
        good = make_schedule_error_comment(
            template_id="T-1", reason="invalid_cron"
        )
        latest = parse_latest_schedule_error([_comment(bad), _comment(good)])
        assert latest is not None
        assert latest["reason"] == "invalid_cron"

    def test_forward_compat_unknown_fields(self):
        raw = (
            '<!-- stokowski:schedule_error {"template":"T-1",'
            '"reason":"invalid_cron","details":null,'
            '"timestamp":"2026-04-19T08:00:00+00:00","future":123} -->'
        )
        latest = parse_latest_schedule_error([_comment(raw)])
        assert latest is not None
        assert latest["future"] == 123


# ---------------------------------------------------------------------------
# Integration: get_comments_since filter semantics
# ---------------------------------------------------------------------------


class TestGetCommentsSinceFiltersWatermarks:
    def test_fired_comments_excluded(self):
        fired = make_fired_comment(
            template_id="T-1",
            slot="2026-04-19T08:00:00+00:00",
            status="child",
            child_id="SMI-201",
        )
        bounded = make_bounded_drop_comment(
            template_id="T-1",
            dropped_count=1,
            earliest_slot="a",
            latest_slot="b",
        )
        sched_err = make_schedule_error_comment(
            template_id="T-1", reason="invalid_cron"
        )
        user_comment = {
            "body": "Please fix the cron expression",
            "createdAt": "2026-04-19T10:00:00+00:00",
        }
        comments = [
            {"body": fired, "createdAt": "2026-04-19T09:00:00+00:00"},
            {"body": bounded, "createdAt": "2026-04-19T09:01:00+00:00"},
            {"body": sched_err, "createdAt": "2026-04-19T09:02:00+00:00"},
            user_comment,
        ]
        result = get_comments_since(comments, "2026-04-19T08:00:00+00:00")
        # Only the plain user comment should survive the filter.
        assert result == [user_comment]

    def test_fired_comments_excluded_without_since(self):
        fired = make_fired_comment(
            template_id="T-1",
            slot="s1",
            status="pending",
        )
        user_comment = {
            "body": "regular feedback",
            "createdAt": "2026-04-19T10:00:00+00:00",
        }
        comments = [
            {"body": fired, "createdAt": "2026-04-19T09:00:00+00:00"},
            user_comment,
        ]
        # since=None → date filter disabled, stokowski:* filter still active.
        result = get_comments_since(comments, None)
        assert result == [user_comment]
