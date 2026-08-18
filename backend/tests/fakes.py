"""Test-suite alias for the shared in-memory twin.

The fakes moved to `commitmentos.sandbox.twin` when the judge sandbox began
running the real command stack over the same twin in production. This module
keeps the test suite's historical `from fakes import ...` imports working.
"""

from __future__ import annotations

from commitmentos.application.ports.gmail_reader import (
    SourceAuthorizationError,
    SourceCursorInvalidError,
)
from commitmentos.sandbox.twin import (
    FakeCalendar,
    FakeCalendarReader,
    FakeCalendarWriter,
    FakeClock,
    FakeGmailReader,
    FakeModelInterpreter,
    FakeTaskDispatcher,
    InMemoryContext,
    InMemoryUnitOfWork,
    SequentialIdGenerator,
)

__all__ = [
    "FakeCalendar",
    "FakeCalendarReader",
    "FakeCalendarWriter",
    "FakeClock",
    "FakeGmailReader",
    "FakeModelInterpreter",
    "FakeTaskDispatcher",
    "InMemoryContext",
    "InMemoryUnitOfWork",
    "SequentialIdGenerator",
    "SourceAuthorizationError",
    "SourceCursorInvalidError",
]
