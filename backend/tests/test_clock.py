"""Phase 1: FakeClock ordering, cancellation, ties (E23), and RealClock."""
import asyncio

import pytest

from app.clock import FakeClock, RealClock


def test_scheduled_callbacks_run_in_due_time_order(fake_clock: FakeClock) -> None:
    order: list[str] = []
    fake_clock.schedule(200, lambda: order.append("second"))
    fake_clock.schedule(50, lambda: order.append("first"))

    fake_clock.advance(200)

    assert order == ["first", "second"]


def test_advance_only_runs_callbacks_due_within_the_advanced_window(fake_clock: FakeClock) -> None:
    ran: list[str] = []
    fake_clock.schedule(100, lambda: ran.append("in-window"))
    fake_clock.schedule(500, lambda: ran.append("out-of-window"))

    fake_clock.advance(100)

    assert ran == ["in-window"]
    assert fake_clock.now_ms() == 100


def test_advance_moves_time_forward_even_with_no_pending_callbacks(fake_clock: FakeClock) -> None:
    fake_clock.advance(1000)
    assert fake_clock.now_ms() == 1000


def test_cancelled_callback_never_runs(fake_clock: FakeClock) -> None:
    ran: list[str] = []
    handle = fake_clock.schedule(100, lambda: ran.append("should-not-run"))

    fake_clock.cancel(handle)
    fake_clock.advance(200)

    assert ran == []


def test_cancel_of_a_foreign_handle_is_a_silent_no_op() -> None:
    other_clock = FakeClock()
    handle = other_clock.schedule(10, lambda: None)
    my_clock = FakeClock()

    # cancel() only flips a flag on the handle itself, so a handle that was
    # never scheduled on my_clock's heap does nothing observable here — it
    # neither raises nor affects my_clock's own callbacks.
    my_clock.cancel(handle)
    my_clock.advance(100)


def test_callbacks_due_at_the_same_ms_run_in_insertion_order(fake_clock: FakeClock) -> None:
    # E23
    order: list[str] = []
    fake_clock.schedule(50, lambda: order.append("first-scheduled"))
    fake_clock.schedule(50, lambda: order.append("second-scheduled"))
    fake_clock.schedule(50, lambda: order.append("third-scheduled"))

    fake_clock.advance(50)

    assert order == ["first-scheduled", "second-scheduled", "third-scheduled"]


def test_a_callback_can_reschedule_at_zero_delay_within_the_same_advance(fake_clock: FakeClock) -> None:
    order: list[str] = []

    def first() -> None:
        order.append("first")
        fake_clock.schedule(0, lambda: order.append("chained"))

    fake_clock.schedule(10, first)
    fake_clock.advance(10)

    assert order == ["first", "chained"]


def test_now_ms_reflects_the_due_time_of_the_running_callback(fake_clock: FakeClock) -> None:
    seen_times: list[int] = []
    fake_clock.schedule(30, lambda: seen_times.append(fake_clock.now_ms()))
    fake_clock.schedule(90, lambda: seen_times.append(fake_clock.now_ms()))

    fake_clock.advance(90)

    assert seen_times == [30, 90]


def test_run_until_quiet_drains_chained_schedules(fake_clock: FakeClock) -> None:
    order: list[int] = []

    def tick(n: int) -> None:
        order.append(n)
        if n < 3:
            fake_clock.schedule(100, lambda: tick(n + 1))

    fake_clock.schedule(100, lambda: tick(1))
    fake_clock.run_until_quiet()

    assert order == [1, 2, 3]
    assert fake_clock.now_ms() == 300


def test_run_until_quiet_is_a_no_op_when_nothing_is_scheduled(fake_clock: FakeClock) -> None:
    fake_clock.run_until_quiet()
    assert fake_clock.now_ms() == 0


def test_negative_delay_is_rejected(fake_clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        fake_clock.schedule(-1, lambda: None)


def test_negative_advance_is_rejected(fake_clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        fake_clock.advance(-1)


def test_real_clock_schedules_via_the_event_loop() -> None:
    loop = asyncio.new_event_loop()
    try:
        clock = RealClock(loop)
        ran: list[bool] = []
        clock.schedule(10, lambda: ran.append(True))

        loop.run_until_complete(asyncio.sleep(0.05))

        assert ran == [True]
    finally:
        loop.close()


def test_real_clock_cancel_prevents_the_callback_from_running() -> None:
    loop = asyncio.new_event_loop()
    try:
        clock = RealClock(loop)
        ran: list[bool] = []
        handle = clock.schedule(10, lambda: ran.append(True))
        clock.cancel(handle)

        loop.run_until_complete(asyncio.sleep(0.05))

        assert ran == []
    finally:
        loop.close()


def test_real_clock_cancels_a_handle_from_any_event_loop() -> None:
    """uvloop returns its own TimerHandle from call_later, and it is *not* a
    subclass of asyncio.TimerHandle. Type-checking against that concrete class
    passed every test and every Windows dev run, then raised TypeError on the
    first redirect inside the Linux container, where uvicorn[standard] installs
    uvloop. The contract is "something cancellable", so check for that.
    """
    from app.clock import RealClock

    class ForeignTimerHandle:
        """Stands in for uvloop.loop.TimerHandle: cancellable, unrelated type."""

        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    clock = RealClock(loop=asyncio.new_event_loop())
    handle = ForeignTimerHandle()
    clock.cancel(handle)
    assert handle.cancelled


def test_real_clock_still_rejects_a_fake_clock_handle() -> None:
    """The type check's real job: catching a FakeClock handle handed to a
    RealClock, which is a genuine programming error."""
    from app.clock import RealClock

    fake = FakeClock()
    handle = fake.schedule(10, lambda: None)
    with pytest.raises(TypeError):
        RealClock(loop=asyncio.new_event_loop()).cancel(handle)
