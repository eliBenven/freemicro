"""Tests for the cached frontmost-app lookup that per-app profiles resolve on.

The real OS lookup is never exercised here: the whole point of the watcher is
that its provider is injectable, so the throttling and degradation can be proven
from data without a running Mac. :func:`detect_frontmost` itself is only checked
to the extent that it never raises and answers ``None`` off macOS.
"""

from __future__ import annotations

from freemicro.input.frontmost import FrontmostWatcher, detect_frontmost


class _Clock:
    """A hand-cranked monotonic clock."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_detect_frontmost_never_raises():
    # On CI this is usually not macOS, so it returns None; on a dev Mac it
    # returns a name. Either way it must not raise.
    result = detect_frontmost()
    assert result is None or isinstance(result, str)


def test_first_poll_reads_the_provider():
    watcher = FrontmostWatcher(lambda: "Terminal", clock=_Clock(), interval=0.3)
    assert watcher.current is None
    assert watcher.poll() == "Terminal"
    assert watcher.current == "Terminal"


def test_poll_is_throttled_to_the_interval():
    clock = _Clock()
    names = iter(["Terminal", "Google Chrome", "Finder"])
    calls = []

    def provider():
        calls.append(clock.t)
        return next(names)

    watcher = FrontmostWatcher(provider, clock=clock, interval=0.3)
    assert watcher.poll() == "Terminal"      # first read
    clock.advance(0.1)
    assert watcher.poll() == "Terminal"      # inside the window: cached
    clock.advance(0.1)
    assert watcher.poll() == "Terminal"      # still inside: cached
    assert len(calls) == 1
    clock.advance(0.2)                        # now 0.4s since the read
    assert watcher.poll() == "Google Chrome"  # window elapsed: re-read
    assert len(calls) == 2


def test_zero_interval_reads_every_poll():
    clock = _Clock()
    names = iter(["A", "B", "C"])
    watcher = FrontmostWatcher(lambda: next(names), clock=clock, interval=0.0)
    assert watcher.poll() == "A"
    assert watcher.poll() == "B"
    assert watcher.poll() == "C"


def test_empty_name_is_normalised_to_none():
    watcher = FrontmostWatcher(lambda: "", clock=_Clock(), interval=0.0)
    assert watcher.poll() is None


def test_a_raising_provider_degrades_to_none():
    def boom():
        raise RuntimeError("no window server")

    watcher = FrontmostWatcher(boom, clock=_Clock(), interval=0.0)
    assert watcher.poll() is None
    assert watcher.current is None
