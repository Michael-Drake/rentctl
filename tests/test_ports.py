"""Unit tests for the pure port draw (``core/ports.py``, ADR-0004)."""

from __future__ import annotations

from rentctl.core.ports import draw_port

BLOCK = 5180
SIZE = 10


def draw(preferred: int, taken=()):
    return draw_port(BLOCK, SIZE, preferred, set(taken))


def test_preference_honoured_when_free():
    d = draw(0)
    assert d.port == 5180
    assert d.preferred is True


def test_non_zero_preference_honoured():
    assert draw(3).port == 5183


def test_falls_back_to_lowest_free_when_preference_taken():
    """A second worktree quietly gets the next port instead of failing."""
    d = draw(0, taken=[5180])
    assert d.port == 5181
    assert d.preferred is False


def test_reuses_a_gap_before_growing_the_range():
    assert draw(0, taken=[5180, 5181, 5183]).port == 5182


def test_third_instance_gets_the_third_port():
    assert draw(0, taken=[5180, 5181]).port == 5182


def test_exhausted_block_returns_none():
    assert draw(0, taken=range(5180, 5190)) is None


def test_full_except_one_finds_it():
    taken = set(range(5180, 5190)) - {5187}
    assert draw(0, taken=taken).port == 5187


def test_preference_outside_the_block_is_ignored_not_drawn():
    """A preferred_offset can only come from a validated registry, but the draw
    is defensive: an out-of-block preference falls back rather than handing out
    a port the project does not own."""
    d = draw_port(BLOCK, SIZE, 99, set())
    assert d.port == 5180
    assert d.preferred is False


def test_does_not_borrow_from_the_neighbouring_block():
    """Exhaustion is loud. Silently taking 5190 would trade a visible failure for
    an invisible cross-project collision."""
    assert draw(0, taken=range(5180, 5190)) is None
