from __future__ import annotations

import base64
import json
from datetime import datetime

from commitmentos.application.dto import Page
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork


class ListActivity:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def execute(
        self,
        user_id: str,
        before: datetime | None,
        limit: int,
    ) -> Page:
        async def _load(repositories: RepositorySet):  # noqa: ANN202
            return tuple(
                await repositories.activity.list_for_user(user_id, before, limit)
            )

        events = await self._unit_of_work.read(_load)
        items = tuple(
            {
                "activity_event_id": event.activity_event_id,
                "event_type": event.event_type.value,
                "summary": event.summary,
                "actor": event.actor,
                "trace_id": event.trace_id,
                "payload": dict(event.payload),
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        )
        next_cursor = (
            self._encode_cursor(events[-1].created_at, events[-1].activity_event_id)
            if len(events) == limit
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    def _encode_cursor(self, occurred_at: datetime, activity_id: str) -> str:
        raw = json.dumps(
            {"occurred_at": occurred_at.isoformat(), "activity_id": activity_id},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def _decode_cursor(self, cursor: str) -> tuple[datetime, str]:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        return datetime.fromisoformat(value["occurred_at"]), str(value["activity_id"])
