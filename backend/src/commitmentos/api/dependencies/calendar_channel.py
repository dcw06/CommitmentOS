from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import UnitOfWork


@dataclass(frozen=True, slots=True)
class CalendarChannelContext:
    user_id: str
    calendar_id: str
    channel_id: str
    resource_id: str
    message_number: str


class CalendarChannelVerificationError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CalendarChannelVerifier:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        clock: Clock,
        maximum_signals_per_window: int,
        rate_limit_window_seconds: int,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._maximum_signals_per_window = maximum_signals_per_window
        self._rate_limit_window_seconds = rate_limit_window_seconds

    async def verify(
        self,
        method: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> CalendarChannelContext:
        if method.upper() != "POST":
            raise CalendarChannelVerificationError(405, "method not allowed")
        if body:
            raise CalendarChannelVerificationError(400, "unexpected body")
        normalized = {key.lower(): value for key, value in headers.items()}
        channel_id = normalized.get("x-goog-channel-id", "")
        resource_id = normalized.get("x-goog-resource-id", "")
        token = normalized.get("x-goog-channel-token", "")
        message_number = normalized.get("x-goog-message-number", "")
        if not token:
            raise CalendarChannelVerificationError(401, "missing channel token")
        if not channel_id or not resource_id or not message_number:
            raise CalendarChannelVerificationError(400, "missing Calendar channel headers")

        async def _load(repositories):
            return await repositories.calendar_channels.get_by_channel_id(channel_id)

        channel = await self._unit_of_work.read(_load)
        if channel is None:
            raise CalendarChannelVerificationError(403, "unknown channel")
        self._constant_time_token_check(token, str(channel.get("token_hash") or ""))
        is_current = channel_id == channel.get("channel_id")
        expected_status = (
            channel.get("status") if is_current else channel.get("previous_status")
        )
        if expected_status not in ("active", "overlap"):
            raise CalendarChannelVerificationError(403, "inactive channel")
        expiration = (
            channel.get("expiration")
            if is_current
            else channel.get("previous_expiration")
        )
        if self._expiration(expiration) <= self._clock.now().astimezone(timezone.utc):
            raise CalendarChannelVerificationError(403, "expired channel")
        expected_resource = (
            channel.get("resource_id")
            if is_current
            else channel.get("previous_resource_id")
        )
        if not expected_resource or resource_id != expected_resource:
            raise CalendarChannelVerificationError(403, "unknown resource")
        await self._consume_rate_limit(channel_id)
        return CalendarChannelContext(
            user_id=str(channel["user_id"]),
            calendar_id=str(channel["calendar_id"]),
            channel_id=channel_id,
            resource_id=resource_id,
            message_number=message_number,
        )

    def _constant_time_token_check(self, presented_token: str, expected_hash: str) -> None:
        presented_hash = hashlib.sha256(presented_token.encode()).hexdigest()
        if not expected_hash or not hmac.compare_digest(presented_hash, expected_hash):
            raise CalendarChannelVerificationError(403, "invalid channel token")

    async def _consume_rate_limit(self, channel_id: str) -> None:
        async def _consume(repositories):
            return await repositories.calendar_channels.consume_rate_limit(
                channel_id,
                self._clock.now(),
                self._rate_limit_window_seconds,
                self._maximum_signals_per_window,
            )

        consumed = await self._unit_of_work.run(_consume)
        if not consumed:
            raise CalendarChannelVerificationError(429, "rate limited")

    @staticmethod
    def _expiration(value: object) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise CalendarChannelVerificationError(
                    403, "invalid channel expiration"
                ) from error
        else:
            raise CalendarChannelVerificationError(403, "missing channel expiration")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise CalendarChannelVerificationError(403, "invalid channel expiration")
        return parsed.astimezone(timezone.utc)
