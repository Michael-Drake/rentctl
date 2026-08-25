"""Tests for the append-only lease event log (``core/events.py``).

The log's whole reason to exist is that a teardown leaves no trace: these tests
pin the three facts a lease file could never carry — the reason, the layer, and
whether devctl actually signalled anything — plus the two properties that make
the file trustworthy in production: it fails open, and it stays bounded.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rentctl.core import events as ev
from rentctl.core.events import EventLog, parse_since, summarize

CDT = timezone(timedelta(hours=-5))
T0 = datetime(2026, 7, 29, 9, 0, tzinfo=CDT)


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def log(tmp_path: Path, clock):
    return EventLog(tmp_path / "events.jsonl", now_fn=clock)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- writing --------------------------------------------------------------

def test_record_up_writes_one_json_line(log, clock):
    log.record_up(
        "webapp",
        profile="default",
        port=5180,
        pid=4242,
        session="sess-1",
        cwd="/proj/webapp",
        lease_expires="2026-07-29T11:00:00-05:00",
        already_running=False,
    )
    (rec,) = _lines(log.path)
    assert rec["event"] == "up"
    assert rec["ts"] == T0.isoformat()
    assert rec["project"] == "webapp"
    assert rec["port"] == 5180
    assert rec["already_running"] is False


def test_records_append_never_overwrite(log, clock):
    log.record_up(
        "webapp",
        profile="default",
        port=5180,
        pid=1,
        session="s",
        cwd="/p",
        lease_expires="x",
        already_running=False,
    )
    clock.advance(minutes=5)
    log.record_down(
        "webapp", op="down", reason=ev.EXPLICIT, reason_source=ev.DECLARED, killed=True
    )
    assert [r["event"] for r in _lines(log.path)] == ["up", "down"]


@pytest.mark.parametrize(
    "reason,expected_layer",
    [
        (ev.EXPLICIT, 1),
        (ev.SESSION_END, 2),
        (ev.EXPIRY, 3),
        (ev.PROCESS_GONE, 3),
        (ev.SWEEP_EXPIRED, 4),
        (ev.SWEEP_DEAD, 4),
    ],
)
def test_layer_is_derived_from_reason(log, reason, expected_layer):
    """The reason→layer mapping is the module's, not the caller's — a caller
    cannot claim a reason from one layer and stamp it with another."""
    log.record_down("webapp", op="down", reason=reason, reason_source=ev.DECLARED, killed=True)
    assert _lines(log.path)[0]["layer"] == expected_layer


def test_killed_distinguishes_kill_from_lease_cleanup(log):
    log.record_down(
        "webapp", op="watchdog", reason=ev.EXPIRY, reason_source=ev.DECLARED, killed=True
    )
    log.record_down(
        "webapp", op="sweep", reason=ev.SWEEP_DEAD, reason_source=ev.DECLARED, killed=False
    )
    killed, cleaned = _lines(log.path)
    assert killed["killed"] is True
    assert cleaned["killed"] is False


def test_none_fields_are_omitted_not_null(log):
    log.record_down(
        "webapp", op="down", reason=ev.EXPLICIT, reason_source=ev.DECLARED, killed=False
    )
    rec = _lines(log.path)[0]
    assert "port" not in rec and "pid" not in rec


def test_record_fails_open_on_unwritable_path(tmp_path, clock):
    """A logging failure must never propagate into a teardown."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    log = EventLog(blocked / "events.jsonl", now_fn=clock)
    assert log.record_up(
        "webapp",
        profile="default",
        port=1,
        pid=1,
        session="s",
        cwd="/p",
        lease_expires="x",
        already_running=False,
    ) is False


# --- retention ------------------------------------------------------------

def _write_one(log: EventLog, clock: Clock) -> None:
    log.record_down(
        "webapp", op="down", reason=ev.EXPLICIT, reason_source=ev.DECLARED, killed=True
    )
    clock.advance(minutes=1)


def test_rotates_at_cap_and_discards_the_older_generation(tmp_path, clock):
    """Growth is bounded by code: past two generations, history is dropped."""
    log = EventLog(tmp_path / "events.jsonl", max_bytes=300, now_fn=clock)
    for _ in range(40):
        _write_one(log, clock)
    assert (tmp_path / "events.jsonl.1").exists()
    assert log.path.stat().st_size < 600          # current generation stays capped
    assert 0 < len(log.read()) < 40               # the oldest events are gone, by design


def test_read_spans_the_rotation_boundary(tmp_path, clock):
    """Rotation is invisible to the reader: one oldest-first stream across both files."""
    log = EventLog(tmp_path / "events.jsonl", max_bytes=10**6, now_fn=clock)
    for _ in range(3):
        _write_one(log, clock)
    log.max_bytes = 1        # next write finds the file over cap → rotates
    _write_one(log, clock)
    log.max_bytes = 10**6    # and the new generation grows normally
    for _ in range(2):
        _write_one(log, clock)

    assert (tmp_path / "events.jsonl.1").exists()
    stamps = [e["ts"] for e in log.read()]
    assert len(stamps) == 6
    assert stamps == sorted(stamps)


# --- reading --------------------------------------------------------------

def _seed(log: EventLog, clock: Clock) -> None:
    log.record_up(
        "webapp",
        profile="default",
        port=5180,
        pid=1,
        session="s",
        cwd="/p",
        lease_expires="x",
        already_running=False,
    )
    clock.advance(hours=1)
    log.record_down(
        "webapp", op="down", reason=ev.SESSION_END, reason_source=ev.DECLARED, killed=True
    )
    clock.advance(hours=1)
    log.record_up(
        "sampleapp",
        profile="default",
        port=5210,
        pid=2,
        session="s",
        cwd="/p2",
        lease_expires="x",
        already_running=False,
    )


def test_read_filters_by_project(log, clock):
    _seed(log, clock)
    assert {e["project"] for e in log.read(project="webapp")} == {"webapp"}


def test_read_filters_by_since(log, clock):
    _seed(log, clock)
    recent = log.read(since=T0 + timedelta(minutes=90))
    assert [e["project"] for e in recent] == ["sampleapp"]


def test_read_limit_keeps_the_newest(log, clock):
    _seed(log, clock)
    assert [e["project"] for e in log.read(limit=1)] == ["sampleapp"]


def test_read_limit_zero_is_empty(log, clock):
    _seed(log, clock)
    assert log.read(limit=0) == []


def test_read_missing_file_is_empty(tmp_path):
    assert EventLog(tmp_path / "nope.jsonl").read() == []


def test_read_skips_torn_lines(log, clock):
    """An interrupted write leaves a partial line; it must not break the reader."""
    _seed(log, clock)
    with open(log.path, "a") as f:
        f.write('{"ts": "2026-07-29T12:00:00-05:00", "event": "do\n')
        f.write("[1, 2, 3]\n")  # valid JSON, wrong shape
    assert len(log.read()) == 3


def test_read_ignores_blank_lines_and_non_string_timestamps(log, clock):
    _seed(log, clock)
    with open(log.path, "a") as f:
        f.write("\n   \n")
        f.write(json.dumps({"ts": 1784080000, "event": "down", "project": "x"}) + "\n")
    assert len(log.read()) == 4
    assert len(log.read(since=T0 - timedelta(days=1))) == 3  # no usable ts → not in the window


def test_read_tolerates_unparseable_timestamp(log, clock):
    _seed(log, clock)
    with open(log.path, "a") as f:
        f.write(json.dumps({"ts": "not-a-time", "event": "down", "project": "x"}) + "\n")
    # Unfiltered it is present; a time filter cannot place it, so it is excluded.
    assert len(log.read()) == 4
    assert len(log.read(since=T0 - timedelta(days=1))) == 3


# --- summarize ------------------------------------------------------------

def test_summarize_separates_declared_from_inferred(log, clock):
    log.record_down(
        "webapp", op="down", reason=ev.SESSION_END, reason_source=ev.DECLARED, killed=True
    )
    clock.advance(minutes=1)
    log.record_down(
        "webapp", op="down", reason=ev.SESSION_END, reason_source=ev.INFERRED, killed=True
    )
    layer2 = summarize(log.read())["layers"]["2"]
    assert layer2["count"] == 2
    assert layer2["declared"] == 1
    assert layer2["inferred"] == 1


def test_summarize_reports_layer_coverage_and_window(log, clock):
    _seed(log, clock)
    clock.advance(hours=1)
    log.record_down(
        "sampleapp", op="watchdog", reason=ev.EXPIRY, reason_source=ev.DECLARED, killed=True
    )
    summary = summarize(log.read())
    assert summary["total"] == 4
    assert summary["counts"] == {"up": 2, "down": 2}
    assert set(summary["layers"]) == {"2", "3"}  # layers 1 and 4 never fired
    assert summary["kills"] == 2
    assert summary["lease_cleanups"] == 0
    assert summary["window"]["from"] == T0.isoformat()


def test_summarize_counts_lease_cleanups_apart_from_kills(log, clock):
    log.record_down(
        "webapp", op="sweep", reason=ev.SWEEP_DEAD, reason_source=ev.DECLARED, killed=False
    )
    summary = summarize(log.read())
    assert summary["kills"] == 0
    assert summary["lease_cleanups"] == 1


# --- G1: counting the sessions that used devctl (WI-0036) -----------------

def test_sessions_counts_distinct_takers_not_leases():
    """G1 asks how many SESSIONS used devctl, not how many leases were taken.
    Weather's overnight was 7 ups across 3 lanes — counting leases would have
    scored it as 7 sessions."""
    events = [
        {"event": "up", "session": "s-1"},
        {"event": "up", "session": "s-1"},
        {"event": "up", "session": "s-2"},
        {"event": "down", "session": "s-3"},   # a teardown is not a new session
    ]
    s = summarize(events)["sessions"]
    assert s["distinct"] == 2
    assert s["attributed_leases"] == 3
    assert s["complete"] is True


def test_unattributed_leases_are_counted_by_nobody():
    """The whole point. An unattributed lease must not be folded into `distinct`,
    estimated, or quietly dropped — it is reported as a hole in the evidence."""
    events = [
        {"event": "up", "session": "s-1"},
        {"event": "up", "session": ev.UNATTRIBUTED},
        {"event": "up"},                        # field absent entirely
        {"event": "up", "session": ""},         # present but empty
    ]
    s = summarize(events)["sessions"]
    assert s["distinct"] == 1
    assert s["attributed_leases"] == 1
    assert s["unattributed_leases"] == 3
    assert s["complete"] is False
    assert "NOBODY" in s["note"]


def test_a_partial_count_cannot_be_read_as_a_total():
    """`complete` is what stops `distinct: 4` being read as "G1 is nearly met"
    when devctl could only see 4 of 40. A count over partial data and a count
    over whole data must not print the same shape."""
    partial = summarize([{"event": "up", "session": "s-1"},
                         {"event": "up", "session": ev.UNATTRIBUTED}])["sessions"]
    whole = summarize([{"event": "up", "session": "s-1"}])["sessions"]
    assert partial["distinct"] == whole["distinct"] == 1
    assert partial["complete"] is False and whole["complete"] is True
    assert partial["note"] != whole["note"]


def test_the_pilots_own_log_shape_scores_zero_sessions():
    """Every event devctl recorded before WI-0036 looked like this. The summary
    must report it as zero-with-a-hole, never as zero-because-nobody-used-it."""
    s = summarize([{"event": "up", "session": "unknown"} for _ in range(16)])["sessions"]
    assert s["distinct"] == 0
    assert s["unattributed_leases"] == 16
    assert s["complete"] is False


def test_summarize_empty_is_well_formed():
    summary = summarize([])
    assert summary == {
        "total": 0,
        "window": None,
        "counts": {},
        "layers": {},
        "kills": 0,
        "lease_cleanups": 0,
        "sessions": {
            "distinct": 0,
            "attributed_leases": 0,
            "unattributed_leases": 0,
            # No leases means nothing unattributed, so the window IS complete —
            # zero of zero. The distinction that matters is against a window with
            # leases devctl could not attribute, which `complete: false` marks.
            "complete": True,
            "note": summary["sessions"]["note"],
        },
        "false_kills": {
            "reported": 0,
            "unmatched": 0,
            "kills_in_window": 0,
            "basis": "self-reported",
            "note": summary["false_kills"]["note"],
        },
    }


# --- G3's channel: the criterion devctl cannot observe (WI-0016) ----------

def test_a_false_kill_report_is_recorded(log):
    log.record_false_kill("webapp", note="killed my vite while I was using it", port=5180)
    written = _lines(log.path)[-1]
    assert written["event"] == ev.FALSE_KILL
    assert written["project"] == "webapp"
    assert written["note"].startswith("killed my vite")
    assert written["port"] == 5180


def test_an_unmatched_report_is_flagged_as_unmatched(log):
    """A report devctl could not tie to any teardown must stay visibly different
    from one it could — it may mean the process was never devctl's."""
    log.record_false_kill("webapp", note="something died", matched=None)
    written = _lines(log.path)[-1]
    assert written["unmatched"] is True
    assert "matched" not in written  # None fields are dropped, not stored as null


def test_a_users_own_words_are_stored_verbatim(log):
    """G3's note is the one input the pilot cannot reconstruct from anything
    else — it is what a person typed. Stored escaped, it is degraded evidence
    in exactly the place there is no second copy."""
    log.record_false_kill("webapp", note="killed my server — mid-debug, très annoying")
    raw = log.path.read_text(encoding="utf-8")
    assert "—" in raw and "très" in raw
    assert "\\u2014" not in raw
    assert _lines(log.path)[-1]["note"].endswith("très annoying")


def test_summarize_never_reports_zero_reports_as_a_pass():
    """The load-bearing property. Every other criterion folds evidence devctl
    gathered; G3 folds the ABSENCE of complaints, and absence of complaint is not
    evidence of absence. A bare 0 would certify the gate by silence — the exact
    ADR-0008 fold, in the surface that scores the gate."""
    summary = summarize([])
    g3 = summary["false_kills"]
    assert g3["reported"] == 0
    assert g3["basis"] == "self-reported"
    assert "not that no false kill occurred" in g3["note"].lower()
    assert "pass" not in g3  # there is no pass/fail verdict to misread


def test_summarize_counts_reports_against_the_kills_they_dispute():
    events = [
        {"event": "down", "project": "webapp", "killed": True, "layer": 2, "ts": "2026-08-01T01:00:00"},
        {"event": "down", "project": "webapp", "killed": True, "layer": 3, "ts": "2026-08-01T02:00:00"},
        {"event": ev.FALSE_KILL, "project": "webapp", "unmatched": True, "ts": "2026-08-01T03:00:00"},
    ]
    g3 = summarize(events)["false_kills"]
    assert g3["reported"] == 1
    assert g3["unmatched"] == 1
    assert g3["kills_in_window"] == 2  # a rate, not a bare count


def test_summarize_ignores_a_down_with_no_layer(log):
    """Hand-written or future-version records without a layer are counted in the
    totals but cannot silently land in a layer bucket."""
    log.record(ev.DOWN, "webapp", killed=True)
    summary = summarize(log.read())
    assert summary["counts"]["down"] == 1
    assert summary["layers"] == {}


# --- parse_since ----------------------------------------------------------

@pytest.mark.parametrize(
    "raw,delta",
    [("7d", timedelta(days=7)), ("24h", timedelta(hours=24)), ("90m", timedelta(minutes=90))],
)
def test_parse_since_relative(raw, delta):
    assert parse_since(raw, T0) == T0 - delta


def test_parse_since_iso_timestamp():
    assert parse_since("2026-07-23T00:00:00-05:00", T0) == datetime(2026, 7, 23, tzinfo=CDT)


def test_parse_since_bare_date_gets_local_offset():
    parsed = parse_since("2026-07-23", T0)
    assert parsed.tzinfo is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 7, 23)


def test_parse_since_rejects_prose():
    with pytest.raises(ValueError, match="unrecognized"):
        parse_since("yesterday", T0)
