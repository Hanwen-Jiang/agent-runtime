"""FakeClock 与 FaultInjector 本身的行为。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agentruntime_core.errors import ErrorCategory, TransientError
from agentruntime_core.testing import FakeClock, FaultInjector


async def test_fake_clock_only_moves_on_advance():
    clock = FakeClock()
    start = clock.now()
    await asyncio.sleep(0.01)
    assert clock.now() == start
    clock.advance(timedelta(seconds=5))
    assert clock.now() - start == timedelta(seconds=5)


async def test_fake_clock_sleep_wakes_after_advance():
    clock = FakeClock()
    sleeper = asyncio.create_task(clock.sleep(timedelta(seconds=10)))
    await asyncio.sleep(0)
    clock.advance(timedelta(seconds=9))
    await asyncio.sleep(0)
    assert not sleeper.done()
    clock.advance(timedelta(seconds=1))
    await asyncio.wait_for(sleeper, timeout=1)


def test_fake_clock_cannot_go_back():
    with pytest.raises(ValueError):
        FakeClock().advance(timedelta(seconds=-1))


def test_fault_injector_fails_next_calls_only():
    faults = FaultInjector()
    faults.fail_next("get", ErrorCategory.TRANSIENT, times=2)
    for _ in range(2):
        with pytest.raises(TransientError):
            faults.check("get")
    faults.check("get")
    faults.check("set")
