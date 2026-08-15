"""Create-after-cancel on a stable event ID revives the reserved corpse.

Google Calendar reserves an event ID permanently: after a cancel, an
`events.insert` with the same ID returns 409 and `events.get` returns a
retrievable corpse with `status == "cancelled"`. The §9.4 stable-identity
contract means a create for the same work block must revive that reserved
event with the desired state — never record the corpse as an applied
success (found live 2026-08-15 by the Phase 5B golden campaign, whose
frozen-placement rule recreates identical block and event IDs every run
after the between-runs reset cancels them).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from commitmentos.application.ports.calendar_writer import CalendarMutationOutcomeType
from commitmentos.domain.actions.models import CalendarActionType, CalendarMutation
from commitmentos.infrastructure.google import calendar_writer as writer_module
from commitmentos.infrastructure.google.calendar_writer import GoogleCalendarWriter

DESIRED_START = datetime(2026, 8, 15, 21, 45, tzinfo=timezone.utc)
DESIRED_END = datetime(2026, 8, 15, 22, 45, tzinfo=timezone.utc)
OWNED_PROPERTIES = {
    "managed_by": "commitmentos",
    "commitment_id": "commitment-1",
    "work_block_id": "block-1",
    "plan_revision": "1",
}


def _mutation() -> CalendarMutation:
    return CalendarMutation(
        action_type=CalendarActionType.INSERT,
        calendar_id="primary",
        calendar_event_id="stableeventid",
        work_block_id="block-1",
        desired_start=DESIRED_START,
        desired_end=DESIRED_END,
        expected_observed_event_etag=None,
        private_properties=OWNED_PROPERTIES,
    )


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


class _Request:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.headers: dict[str, str] = {}
        self._result = result
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


class _Events:
    def __init__(self, existing: dict[str, Any] | None) -> None:
        self._existing = existing
        self.update_calls: list[dict[str, Any]] = []
        self.update_request: _Request | None = None

    def insert(self, calendarId: str, body: dict[str, Any]) -> _Request:  # noqa: N803
        del calendarId, body
        return _Request(error=_http_error(409))

    def get(self, calendarId: str, eventId: str) -> _Request:  # noqa: N803
        del calendarId, eventId
        return _Request(result=self._existing)

    def update(self, calendarId: str, eventId: str, body: dict[str, Any]) -> _Request:  # noqa: N803
        self.update_calls.append({"event_id": eventId, "body": body})
        revived = dict(body)
        revived["id"] = eventId
        revived["etag"] = '"etag-after-revival"'
        del calendarId
        self.update_request = _Request(result=revived)
        return self.update_request


class _Service:
    def __init__(self, events: _Events) -> None:
        self._events = events

    def events(self) -> _Events:
        return self._events


class _Credentials:
    def credentials(self) -> None:
        return None


@pytest.fixture
def corpse() -> dict[str, Any]:
    return {
        "id": "stableeventid",
        "etag": '"corpse-etag"',
        "status": "cancelled",
        "extendedProperties": {"private": dict(OWNED_PROPERTIES)},
    }


def _writer(monkeypatch: pytest.MonkeyPatch, events: _Events) -> GoogleCalendarWriter:
    monkeypatch.setattr(
        writer_module, "build_google_api", lambda *args, **kwargs: _Service(events)
    )
    return GoogleCalendarWriter(_Credentials())  # type: ignore[arg-type]


async def test_owned_cancelled_corpse_is_revived_with_desired_state(
    monkeypatch: pytest.MonkeyPatch, corpse: dict[str, Any]
) -> None:
    events = _Events(existing=corpse)
    writer = _writer(monkeypatch, events)

    outcome = await writer.insert_or_adopt_owned(_mutation())

    assert outcome.outcome_type is CalendarMutationOutcomeType.APPLIED
    assert outcome.event is not None
    assert outcome.event.status == "confirmed"
    assert outcome.event.etag == '"etag-after-revival"'
    assert len(events.update_calls) == 1
    body = events.update_calls[0]["body"]
    assert body["status"] == "confirmed"
    assert body["start"] == {"dateTime": DESIRED_START.isoformat()}
    assert body["end"] == {"dateTime": DESIRED_END.isoformat()}
    assert body["extendedProperties"] == {"private": dict(OWNED_PROPERTIES)}
    assert events.update_request is not None
    assert events.update_request.headers["If-Match"] == '"corpse-etag"'


async def test_live_owned_event_still_adopts_without_update(
    monkeypatch: pytest.MonkeyPatch, corpse: dict[str, Any]
) -> None:
    alive = dict(corpse)
    alive["status"] = "confirmed"
    events = _Events(existing=alive)
    writer = _writer(monkeypatch, events)

    outcome = await writer.insert_or_adopt_owned(_mutation())

    assert outcome.outcome_type is CalendarMutationOutcomeType.ALREADY_APPLIED
    assert events.update_calls == []


async def test_unowned_corpse_is_refused_before_any_revival(
    monkeypatch: pytest.MonkeyPatch, corpse: dict[str, Any]
) -> None:
    foreign = dict(corpse)
    foreign["extendedProperties"] = {"private": {"managed_by": "someone-else"}}
    events = _Events(existing=foreign)
    writer = _writer(monkeypatch, events)

    outcome = await writer.insert_or_adopt_owned(_mutation())

    assert outcome.outcome_type is CalendarMutationOutcomeType.TERMINAL_FAILURE
    assert outcome.error == {"error_code": "ownership_mismatch"}
    assert events.update_calls == []
