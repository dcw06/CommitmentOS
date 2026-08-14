from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Mapping

from googleapiclient.discovery import build as build_google_api
from googleapiclient.errors import HttpError

from commitmentos.application.ports.calendar_reader import CalendarEventRecord
from commitmentos.application.ports.calendar_writer import (
    CalendarMutationOutcome,
    CalendarMutationOutcomeType,
)
from commitmentos.domain.actions.models import CalendarMutation
from commitmentos.infrastructure.google.credentials import ControlledCredentialsProvider

RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _record(calendar_id: str, event: Mapping[str, Any]) -> CalendarEventRecord:
    return CalendarEventRecord(
        calendar_id=calendar_id,
        event_id=event.get("id", ""),
        etag=event.get("etag"),
        status=event.get("status", "unknown"),
        payload=dict(event),
        payload_hash=_payload_hash(event),
    )


def _is_owned(event: Mapping[str, Any], work_block_id: str) -> bool:
    properties = event.get("extendedProperties", {}).get("private", {})
    return (
        properties.get("managed_by") == "commitmentos"
        and properties.get("work_block_id") == work_block_id
    )


class GoogleCalendarWriter:
    """The only concrete adapter allowed to mutate Calendar.

    Insert relies on the stable client-supplied event ID for idempotency and
    may look up the target only for owned-event adoption. Patch and cancel
    always send the outbox's `expected_observed_event_etag` as `If-Match`;
    HTTP 412 maps to typed `PRECONDITION_FAILED` and is never retried here.
    """

    def __init__(self, credentials_provider: ControlledCredentialsProvider) -> None:
        self._credentials_provider = credentials_provider

    def _service(self) -> Any:
        return build_google_api(
            "calendar",
            "v3",
            credentials=self._credentials_provider.credentials(),
            cache_discovery=False,
        )

    @staticmethod
    def _event_body(mutation: CalendarMutation) -> dict[str, Any]:
        return {
            "summary": "CommitmentOS work block",
            "start": {"dateTime": mutation.desired_start.isoformat()},
            "end": {"dateTime": mutation.desired_end.isoformat()},
            "extendedProperties": {"private": dict(mutation.private_properties)},
        }

    @staticmethod
    def _failure(error: HttpError) -> CalendarMutationOutcome:
        status = error.resp.status if error.resp is not None else 0
        outcome_type = (
            CalendarMutationOutcomeType.RETRYABLE_FAILURE
            if status in RETRYABLE_HTTP_STATUSES
            else CalendarMutationOutcomeType.TERMINAL_FAILURE
        )
        if status == 412:
            outcome_type = CalendarMutationOutcomeType.PRECONDITION_FAILED
        return CalendarMutationOutcome(
            outcome_type=outcome_type,
            event=None,
            error={"error_code": f"http_{status}", "reason": str(error)[:500]},
        )

    async def insert_or_adopt_owned(self, mutation: CalendarMutation) -> CalendarMutationOutcome:
        def _blocking() -> CalendarMutationOutcome:
            service = self._service()
            body = self._event_body(mutation)
            body["id"] = mutation.calendar_event_id
            try:
                event = (
                    service.events()
                    .insert(calendarId=mutation.calendar_id, body=body)
                    .execute()
                )
                return CalendarMutationOutcome(
                    outcome_type=CalendarMutationOutcomeType.APPLIED,
                    event=_record(mutation.calendar_id, event),
                    error=None,
                )
            except HttpError as error:
                status = error.resp.status if error.resp is not None else 0
                if status != 409:
                    return self._failure(error)
            # The stable ID already exists: adopt only after verifying ownership.
            try:
                existing = (
                    service.events()
                    .get(calendarId=mutation.calendar_id, eventId=mutation.calendar_event_id)
                    .execute()
                )
            except HttpError as error:
                return self._failure(error)
            if not _is_owned(existing, mutation.work_block_id):
                return CalendarMutationOutcome(
                    outcome_type=CalendarMutationOutcomeType.TERMINAL_FAILURE,
                    event=None,
                    error={"error_code": "ownership_mismatch"},
                )
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.ALREADY_APPLIED,
                event=_record(mutation.calendar_id, existing),
                error=None,
            )

        return await asyncio.to_thread(_blocking)

    async def patch_owned(self, mutation: CalendarMutation) -> CalendarMutationOutcome:
        def _blocking() -> CalendarMutationOutcome:
            service = self._service()
            ownership_failure = self._verify_owned(service, mutation)
            if ownership_failure is not None:
                return ownership_failure
            request = service.events().patch(
                calendarId=mutation.calendar_id,
                eventId=mutation.calendar_event_id,
                body={
                    "start": {"dateTime": mutation.desired_start.isoformat()},
                    "end": {"dateTime": mutation.desired_end.isoformat()},
                    "extendedProperties": {"private": dict(mutation.private_properties)},
                },
            )
            request.headers["If-Match"] = mutation.expected_observed_event_etag
            try:
                event = request.execute()
            except HttpError as error:
                return self._failure(error)
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.APPLIED,
                event=_record(mutation.calendar_id, event),
                error=None,
            )

        return await asyncio.to_thread(_blocking)

    async def cancel_owned(self, mutation: CalendarMutation) -> CalendarMutationOutcome:
        def _blocking() -> CalendarMutationOutcome:
            service = self._service()
            ownership_failure = self._verify_owned(service, mutation)
            if ownership_failure is not None:
                return ownership_failure
            request = service.events().delete(
                calendarId=mutation.calendar_id,
                eventId=mutation.calendar_event_id,
            )
            request.headers["If-Match"] = mutation.expected_observed_event_etag
            try:
                request.execute()
            except HttpError as error:
                return self._failure(error)
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.APPLIED,
                event=None,
                error=None,
            )

        return await asyncio.to_thread(_blocking)

    def _verify_owned(
        self,
        service: Any,
        mutation: CalendarMutation,
    ) -> CalendarMutationOutcome | None:
        """Independent §9.3 refusal: never mutate an event without valid
        CommitmentOS ownership properties."""
        try:
            existing = (
                service.events()
                .get(calendarId=mutation.calendar_id, eventId=mutation.calendar_event_id)
                .execute()
            )
        except HttpError as error:
            status = error.resp.status if error.resp is not None else 0
            if status in (404, 410):
                return CalendarMutationOutcome(
                    outcome_type=CalendarMutationOutcomeType.TERMINAL_FAILURE,
                    event=None,
                    error={"error_code": "event_not_found"},
                )
            return self._failure(error)
        if not _is_owned(existing, mutation.work_block_id):
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.TERMINAL_FAILURE,
                event=None,
                error={"error_code": "ownership_mismatch"},
            )
        return None
