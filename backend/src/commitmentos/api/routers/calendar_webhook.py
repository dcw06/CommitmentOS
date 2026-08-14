from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request, Response

from commitmentos.api.dependencies.calendar_channel import (
    CalendarChannelContext,
    CalendarChannelVerificationError,
    CalendarChannelVerifier,
)
from commitmentos.application.commands.receive_calendar_signal import ReceiveCalendarSignal


class CalendarWebhookRouter:
    def __init__(
        self,
        verifier: CalendarChannelVerifier,
        receive_calendar_signal: ReceiveCalendarSignal,
        webhook_path: str,
    ) -> None:
        self._verifier = verifier
        self._receive_calendar_signal = receive_calendar_signal
        self._webhook_path = webhook_path

    def build(self) -> Any:
        router = APIRouter(tags=["calendar-webhook"])
        handler = self

        @router.post(self._webhook_path, status_code=204)
        async def calendar_webhook(request: Request) -> Response:
            return await handler.receive(
                request.method,
                request.headers,
                await request.body(),
                getattr(request.state, "trace_id", "trace-calendar-webhook"),
            )

        return router

    async def receive(
        self,
        method: str,
        headers: Mapping[str, str],
        body: bytes,
        trace_id: str,
    ) -> Any:
        try:
            context = await self._verifier.verify(method, headers, body)
            return await self._accept_valid_signal(context, headers, trace_id)
        except CalendarChannelVerificationError as error:
            raise HTTPException(
                status_code=error.status_code, detail=error.detail
            ) from error
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    async def _accept_valid_signal(
        self,
        context: CalendarChannelContext,
        headers: Mapping[str, str],
        trace_id: str,
    ) -> Any:
        await self._receive_calendar_signal.execute(
            headers,
            trace_id,
            context.user_id,
            context.calendar_id,
        )
        return Response(status_code=204)
