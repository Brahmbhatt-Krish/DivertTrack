"""Clock abstraction. FakeClock drives every test deterministically; RealClock
drives the live demo off the asyncio event loop. Both satisfy the same
Protocol, so dispatcher.py and every other scheduler never branches on which
one it was given — the fake and the real clock are interchangeable.
"""
from __future__ import annotations

import asyncio
import heapq
from dataclasses import dataclass, field
from itertools import count
from typing import Callable, Optional, Protocol

# Opaque token returned by schedule() and accepted by cancel(). Callers must
# not inspect it — only pass it back to the same clock's cancel().
Handle = object


class Clock(Protocol):
    def now_ms(self) -> int: ...

    def schedule(self, delay_ms: int, fn: Callable[[], None]) -> Handle: ...

    def cancel(self, handle: Handle) -> None: ...


@dataclass(order=True)
class _ScheduledCall:
    """Ordered by (due_ms, seq) so heapq gives us due-time order with ties
    broken by insertion order — that tiebreak is what E23 requires."""

    due_ms: int
    seq: int
    fn: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class FakeClock:
    """A clock that only moves when told to. Every scheduled callback runs
    with now_ms() equal to its own due_ms — a callback that reschedules
    something is scheduling relative to the instant it fired at, not the
    instant advance() was asked to reach.
    """

    def __init__(self, start_ms: int = 0) -> None:
        self._now_ms = start_ms
        self._heap: list[_ScheduledCall] = []
        self._seq = count()

    def now_ms(self) -> int:
        return self._now_ms

    def schedule(self, delay_ms: int, fn: Callable[[], None]) -> Handle:
        if delay_ms < 0:
            raise ValueError(f"Expected a non-negative delay_ms, received {delay_ms}")
        call = _ScheduledCall(due_ms=self._now_ms + delay_ms, seq=next(self._seq), fn=fn)
        heapq.heappush(self._heap, call)
        return call

    def cancel(self, handle: Handle) -> None:
        if not isinstance(handle, _ScheduledCall):
            raise TypeError(
                f"Expected a handle returned by FakeClock.schedule, received {handle!r}"
            )
        handle.cancelled = True

    def advance(self, ms: int) -> None:
        """Move time forward by ms, running every callback due along the way."""
        if ms < 0:
            raise ValueError(f"Expected a non-negative ms, received {ms}")
        target = self._now_ms + ms
        self._drain_until(target)
        self._now_ms = target

    def run_until_quiet(self) -> None:
        """Jump straight from one due timestamp to the next until nothing is
        scheduled — cheap even when a scenario spans several seconds, and
        the only way tests can run a scenario to completion without knowing
        its total duration up front."""
        while self._heap:
            next_due = self._heap[0].due_ms
            self._drain_until(next_due)
            self._now_ms = next_due

    def _drain_until(self, target: int) -> None:
        while self._heap and self._heap[0].due_ms <= target:
            call = heapq.heappop(self._heap)
            if call.cancelled:
                continue
            self._now_ms = call.due_ms
            call.fn()


class RealClock:
    """Wall-clock time in milliseconds, scheduled on an asyncio event loop."""

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._loop = loop or asyncio.get_event_loop()

    def now_ms(self) -> int:
        return int(self._loop.time() * 1000)

    def schedule(self, delay_ms: int, fn: Callable[[], None]) -> Handle:
        if delay_ms < 0:
            raise ValueError(f"Expected a non-negative delay_ms, received {delay_ms}")
        return self._loop.call_later(delay_ms / 1000, fn)

    def cancel(self, handle: Handle) -> None:
        # Duck-typed, not isinstance(handle, asyncio.TimerHandle): uvloop —
        # which uvicorn[standard] installs on Linux but not on Windows —
        # returns its own uvloop.loop.TimerHandle from call_later, and that is
        # not a subclass of asyncio.TimerHandle. The isinstance check
        # therefore passed every test and every local run and blew up only
        # inside the Linux container, on the first redirect.
        #
        # The check still earns its place: its real job is catching a
        # FakeClock handle handed to a RealClock, which is a genuine
        # programming error and stays rejected below.
        if isinstance(handle, _ScheduledCall) or not callable(getattr(handle, "cancel", None)):
            raise TypeError(
                f"Expected a handle returned by RealClock.schedule, received {handle!r}"
            )
        handle.cancel()
