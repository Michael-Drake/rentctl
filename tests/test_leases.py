"""Unit tests for lease serialization, atomic I/O, and pure helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rentctl.core.errors import LEASE_INVALID, DevctlError
from rentctl.core.leases import Lease, list_lease_files
from rentctl.core.models import ProcessHandle

CDT = timezone(timedelta(hours=-5))


def make_lease(**over) -> Lease:
    base = dict(
        project="webapp",
        profile="default",
        runner="process",
        handle={"pid": 4242, "pid_start_time": 1784080000.12},
        port=5180,
        session="abc123",
        cwd="/path/to/exampleorg",
        created=datetime(2026, 7, 14, 7, 30, tzinfo=CDT),
        expires=datetime(2026, 7, 14, 9, 30, tzinfo=CDT),
        log="/logs/webapp-2026-07-14T0730.log",
        watchdog_pid=4250,
    )
    base.update(over)
    return Lease(**base)


def test_dict_round_trip():
    lease = make_lease()
    assert Lease.from_dict(lease.to_dict()) == lease


def test_disk_round_trip(devctl_home):
    lease = make_lease()
    path = devctl_home.lease_file("webapp")
    lease.write(path)
    assert Lease.read(path) == lease
    assert path.exists()


def test_write_is_atomic_no_tmp_left(devctl_home):
    lease = make_lease()
    path = devctl_home.lease_file("webapp")
    lease.write(path)
    # No stray temp files beside the lease.
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == []


def test_read_if_exists_missing_returns_none(devctl_home):
    assert Lease.read_if_exists(devctl_home.lease_file("ghost")) is None


def test_read_corrupt_is_lease_invalid(devctl_home):
    path = devctl_home.lease_file("webapp")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ half written")
    with pytest.raises(DevctlError) as ei:
        Lease.read(path)
    assert ei.value.code == LEASE_INVALID


def test_read_non_object_is_lease_invalid(devctl_home):
    path = devctl_home.lease_file("webapp")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]")
    with pytest.raises(DevctlError) as ei:
        Lease.read(path)
    assert ei.value.code == LEASE_INVALID


def test_from_dict_missing_field_is_lease_invalid():
    with pytest.raises(DevctlError) as ei:
        Lease.from_dict({"project": "webapp"})
    assert ei.value.code == LEASE_INVALID


def test_process_handle():
    lease = make_lease()
    assert lease.process_handle() == ProcessHandle(pid=4242, pid_start_time=1784080000.12)


def test_is_expired():
    lease = make_lease()
    assert lease.is_expired(datetime(2026, 7, 14, 9, 31, tzinfo=CDT)) is True
    assert lease.is_expired(datetime(2026, 7, 14, 9, 30, tzinfo=CDT)) is True  # boundary = expired
    assert lease.is_expired(datetime(2026, 7, 14, 9, 29, tzinfo=CDT)) is False


def test_renewed_pushes_expiry_only():
    lease = make_lease()
    new_exp = datetime(2026, 7, 14, 11, 30, tzinfo=CDT)
    renewed = lease.renewed(new_exp)
    assert renewed.expires == new_exp
    assert renewed.created == lease.created
    assert renewed.handle == lease.handle


def test_with_watchdog():
    lease = make_lease(watchdog_pid=None)
    assert lease.watchdog_pid is None
    assert lease.with_watchdog(9999).watchdog_pid == 9999


def test_watchdog_pid_none_survives_round_trip():
    lease = make_lease(watchdog_pid=None)
    assert Lease.from_dict(lease.to_dict()).watchdog_pid is None


def test_list_lease_files(devctl_home):
    assert list_lease_files(devctl_home.leases_dir) == []
    make_lease(project="webapp").write(devctl_home.lease_file("webapp"))
    make_lease(project="worldcup").write(devctl_home.lease_file("worldcup"))
    files = list_lease_files(devctl_home.leases_dir)
    assert [p.stem for p in files] == ["webapp", "worldcup"]


def test_list_lease_files_missing_dir(tmp_path):
    assert list_lease_files(tmp_path / "nope") == []
