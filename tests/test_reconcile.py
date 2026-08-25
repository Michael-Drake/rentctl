"""Unit tests for the pure reconciler — the kill/keep/clean decision matrix."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rentctl.core.leases import Lease
from rentctl.core.reconcile import Action, decide, plan_reconcile

CDT = timezone(timedelta(hours=-5))
NOW = datetime(2026, 7, 14, 9, 0, tzinfo=CDT)


def lease(project="webapp", *, expires_offset_min: int) -> Lease:
    return Lease(
        project=project,
        profile="default",
        runner="process",
        handle={"pid": 100, "pid_start_time": 1.0},
        port=5180,
        session="s",
        cwd="/x",
        created=NOW - timedelta(hours=1),
        expires=NOW + timedelta(minutes=expires_offset_min),
        log="/l",
    )


ALIVE = lambda _l: True
DEAD = lambda _l: False


# --- the 2x2 matrix (alive? x expired?) -----------------------------------

def test_alive_and_current_is_keep():
    v = decide(lease(expires_offset_min=30), ALIVE, NOW)
    assert v.action is Action.KEEP


def test_alive_and_expired_is_expire():
    v = decide(lease(expires_offset_min=-1), ALIVE, NOW)
    assert v.action is Action.EXPIRE


def test_dead_and_current_is_clean():
    v = decide(lease(expires_offset_min=30), DEAD, NOW)
    assert v.action is Action.CLEAN


def test_dead_and_expired_is_clean():
    # Dead wins over expired: nothing to kill, just remove the lease.
    v = decide(lease(expires_offset_min=-30), DEAD, NOW)
    assert v.action is Action.CLEAN


def test_expiry_boundary_is_expired():
    # now == expires counts as expired (matches Lease.is_expired).
    v = decide(lease(expires_offset_min=0), ALIVE, NOW)
    assert v.action is Action.EXPIRE


def test_verdict_carries_project_and_reason():
    v = decide(lease(project="worldcup", expires_offset_min=30), ALIVE, NOW)
    assert v.project == "worldcup"
    assert v.reason


# --- plan_reconcile over a mixed set --------------------------------------

def test_plan_reconcile_mixed():
    leases = [
        lease(project="keepme", expires_offset_min=60),
        lease(project="expireme", expires_offset_min=-5),
        lease(project="deadme", expires_offset_min=60),
    ]

    def is_alive(l: Lease) -> bool:
        return l.project != "deadme"

    plan = plan_reconcile(leases, is_alive, NOW)
    by_project = {v.project: v.action for v in plan}
    assert by_project == {
        "keepme": Action.KEEP,
        "expireme": Action.EXPIRE,
        "deadme": Action.CLEAN,
    }


def test_plan_reconcile_empty():
    assert plan_reconcile([], ALIVE, NOW) == []
