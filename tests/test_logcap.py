"""Unit tests for the in-place log cap (spec follow-up)."""

from __future__ import annotations

from rentctl.core.logcap import _MARKER, trim_log_if_large


def test_small_log_untouched(tmp_path):
    log = tmp_path / "srv.log"
    log.write_bytes(b"line1\nline2\n")
    assert trim_log_if_large(log, max_bytes=1000, keep_bytes=100) is False
    assert log.read_bytes() == b"line1\nline2\n"


def test_missing_file_is_false(tmp_path):
    assert trim_log_if_large(tmp_path / "nope.log") is False


def test_large_log_trimmed_to_keep(tmp_path):
    log = tmp_path / "srv.log"
    # 100 numbered lines, each 20 bytes → ~2000 bytes.
    lines = [f"line-{i:04d}-padding\n".encode() for i in range(100)]
    log.write_bytes(b"".join(lines))
    original = log.stat().st_size

    trimmed = trim_log_if_large(log, max_bytes=500, keep_bytes=200)
    assert trimmed is True

    data = log.read_bytes()
    assert data.startswith(_MARKER)
    assert log.stat().st_size < original
    # The most recent line survives; an early line is gone.
    assert b"line-0099-padding" in data
    assert b"line-0000-padding" not in data
    # No partial first line after the marker.
    body = data[len(_MARKER):]
    assert body.split(b"\n", 1)[0].startswith(b"line-")


def test_keep_larger_than_size_edge(tmp_path):
    # keep_bytes bigger than the file: seek clamps to 0, whole tail kept.
    log = tmp_path / "srv.log"
    log.write_bytes(b"x" * 600 + b"\nlast\n")
    assert trim_log_if_large(log, max_bytes=100, keep_bytes=10_000) is True
    assert b"last" in log.read_bytes()
