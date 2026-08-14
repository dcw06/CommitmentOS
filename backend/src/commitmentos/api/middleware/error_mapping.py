from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from commitmentos.domain.shared.errors import (
    DomainError,
    InvalidTransitionError,
    RevisionConflictError,
)

logger = logging.getLogger("commitmentos.api")


class ErrorMappingMiddleware(BaseHTTPMiddleware):
    """Maps domain errors to safe JSON responses without leaking internals."""

    async def dispatch(
        self,
        request: Any,
        call_next: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        try:
            return await call_next(request)
        except Exception as error:  # noqa: BLE001 - boundary translation
            trace_id = getattr(request.state, "trace_id", "trace-unset")
            return self._to_response(error, trace_id)

    def _to_response(self, error: Exception, trace_id: str) -> Any:
        error_code = self._safe_error_code(error)
        if isinstance(error, RevisionConflictError):
            status_code = 409
        elif isinstance(error, InvalidTransitionError):
            status_code = 409
        elif isinstance(error, DomainError):
            status_code = 400
        else:
            status_code = 500
            locations: list[str] = []
            traceback_cursor = error.__traceback__
            while traceback_cursor is not None:
                frame = traceback_cursor.tb_frame
                locations.append(f"{frame.f_code.co_name}:{traceback_cursor.tb_lineno}")
                traceback_cursor = traceback_cursor.tb_next
            logger.exception(
                "unhandled error",
                extra={
                    "safe_payload": {
                        "trace_id": trace_id,
                        "error_type": type(error).__name__,
                        "location": " > ".join(locations[-8:]),
                    }
                },
            )
        return JSONResponse(
            status_code=status_code,
            content={"error_code": error_code, "trace_id": trace_id},
        )

    def _safe_error_code(self, error: Exception) -> str:
        if isinstance(error, DomainError):
            return type(error).__name__
        return "internal_error"
