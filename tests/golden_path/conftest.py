"""Golden-campaign rehearsal fixtures over the shared backend twin."""

from __future__ import annotations

import pytest

from tests.fault_injection.harness import harness


@pytest.fixture
def app():
    from fakes import FakeCalendar, FakeClock, FakeTaskDispatcher

    return harness.Phase1App(
        store={},
        clock=FakeClock(),
        task_dispatcher=FakeTaskDispatcher(),
        calendar=FakeCalendar(),
    )
