"""Tests for the CLI shell: subcommand routing, arg passing, exit codes."""

from __future__ import annotations

import json

import pytest

from rentctl import cli


class RecordingEvents:
    """Stand-in for the service's EventLog — records the read filters it was given."""

    def __init__(self, events: list[dict] | None = None):
        self.reads: list[tuple] = []
        self._events = events or []

    def read(self, *, project=None, since=None, limit=None):
        self.reads.append((project, since, limit))
        return self._events


class RecordingService:
    def __init__(self, ok: bool = True, events: list[dict] | None = None):
        self.calls: list[tuple] = []
        self.waits: list[float | None] = []
        self._ok = ok
        self.events = RecordingEvents(events)

    def env_up(self, project, lease_minutes, profile, cwd=None):
        self.calls.append(("up", project, lease_minutes, profile, cwd))
        return {"ok": self._ok, "project": project, "port": 5180}

    def env_down(self, project=None, cwd=None, reason=None, all_instances=False, wait_s=None):
        self.calls.append(("down", project, cwd, reason, all_instances))
        self.waits.append(wait_s)
        return {"ok": True, "was_running": False}

    def env_ls(self):
        self.calls.append(("ls",))
        return {"ok": True, "environments": []}

    def env_sweep(self):
        self.calls.append(("sweep",))
        return {"ok": True, "swept": [], "kept": []}


def test_version_prints_the_single_version_source(capsys):
    """WI-0066. Subcommands are required, so --version has to short-circuit the
    parser before that check -- and must not need a service or any state."""
    import rentctl

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"], service=RecordingService())
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"rentctl {rentctl.__version__}\n"


def test_up_routes_and_passes_args(capsys):
    rec = RecordingService()
    rc = cli.main(
        ["up", "webapp", "--lease-minutes", "30", "--profile", "api-only", "--cwd", "/p"],
        service=rec,
    )
    assert rc == 0
    assert rec.calls == [("up", "webapp", 30, "api-only", "/p")]
    assert json.loads(capsys.readouterr().out)["port"] == 5180


def test_up_defaults(capsys):
    rec = RecordingService()
    cli.main(["up", "webapp"], service=rec)
    assert rec.calls == [("up", "webapp", 120, "default", None)]


def test_up_failure_exit_code():
    rec = RecordingService(ok=False)
    assert cli.main(["up", "webapp"], service=rec) == 1


def test_down_project(capsys):
    rec = RecordingService()
    cli.main(["down", "webapp"], service=rec)
    assert rec.calls == [("down", "webapp", None, None, False)]


def test_down_all_by_cwd(capsys):
    rec = RecordingService()
    cli.main(["down", "--all", "--cwd", "/proj"], service=rec)
    assert rec.calls == [("down", None, "/proj", None, False)]


def test_down_no_args_uses_cwd(capsys):
    rec = RecordingService()
    cli.main(["down"], service=rec)
    assert rec.calls == [("down", None, None, None, False)]


def test_down_passes_declared_reason(capsys):
    """The SessionEnd hook's shape — the reason reaches the service verbatim."""
    rec = RecordingService()
    cli.main(["down", "--all", "--cwd", "/proj", "--reason", "session-end"], service=rec)
    assert rec.calls == [("down", None, "/proj", "session-end", False)]


def test_down_wait_defaults_to_the_services_budget(capsys):
    """No --wait means "let the service choose": 15 s, or 0 for a declared
    session-end (ADR-0016 R0). The CLI does not second-guess it."""
    rec = RecordingService()
    cli.main(["down", "webapp"], service=rec)
    cli.main(["down", "--all", "--reason", "session-end"], service=rec)
    assert rec.waits == [None, None]


def test_down_passes_wait_seconds(capsys):
    """R7: `--wait SECONDS` is public; `--wait 0` is fire and forget."""
    rec = RecordingService()
    cli.main(["down", "webapp", "--wait", "0"], service=rec)
    cli.main(["down", "--all", "--cwd", "/p", "--wait", "2.5"], service=rec)
    assert rec.waits == [0.0, 2.5]


def test_a_pending_down_exits_zero(capsys):
    """`pending` is ok: the request was accepted and cleanup completes regardless."""

    class Pending(RecordingService):
        def env_down(self, *a, **kw):
            return {"ok": True, "stopped": None, "pending": True, "state": "stopping"}

    assert cli.main(["down", "webapp", "--wait", "0"], service=Pending()) == 0
    assert json.loads(capsys.readouterr().out)["pending"] is True


def test_down_rejects_forged_layer_reason(capsys):
    """A CLI caller cannot claim the watchdog or the reconciler fired."""
    rec = RecordingService()
    with pytest.raises(SystemExit):
        cli.main(["down", "webapp", "--reason", "expiry"], service=rec)


def test_down_all_instances_is_opt_in(capsys):
    """Without the flag a named down reaches only this cwd's instance."""
    rec = RecordingService()
    cli.main(["down", "webapp", "--all-instances"], service=rec)
    assert rec.calls == [("down", "webapp", None, None, True)]


def test_ls_routes(capsys):
    rec = RecordingService()
    assert cli.main(["ls"], service=rec) == 0
    assert rec.calls == [("ls",)]


def test_sweep_routes(capsys):
    rec = RecordingService()
    assert cli.main(["sweep"], service=rec) == 0
    assert rec.calls == [("sweep",)]


# --- events ---------------------------------------------------------------

_SAMPLE = [
    {"ts": "2026-07-29T09:00:00-05:00", "event": "up", "project": "webapp"},
    {
        "ts": "2026-07-29T10:00:00-05:00",
        "event": "down",
        "project": "webapp",
        "reason": "session-end",
        "layer": 2,
        "reason_source": "declared",
        "killed": True,
    },
]


def test_events_lists(capsys):
    rec = RecordingService(events=_SAMPLE)
    assert cli.main(["events"], service=rec) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 2
    assert out["events"][1]["reason"] == "session-end"


def test_events_passes_filters(capsys):
    rec = RecordingService(events=_SAMPLE)
    cli.main(["events", "--project", "webapp", "--since", "7d", "--limit", "5"], service=rec)
    project, since, limit = rec.events.reads[0]
    assert project == "webapp"
    assert limit == 5
    assert since is not None  # relative window resolved to an absolute time


def test_events_summary(capsys):
    rec = RecordingService(events=_SAMPLE)
    assert cli.main(["events", "--summary"], service=rec) == 0
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert summary["layers"]["2"]["declared"] == 1
    assert summary["kills"] == 1


def test_events_bad_since_exits_nonzero(capsys):
    rec = RecordingService()
    assert cli.main(["events", "--since", "yesterday"], service=rec) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "BAD_SINCE"
