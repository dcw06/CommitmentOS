from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attaches a trace ID to every request for command and audit plumbing."""

    async def dispatch(
        self,
        request: Any,
        call_next: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        request.state.trace_id = self._resolve_trace_id(request)
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    def _resolve_trace_id(self, request: Any) -> str:
        cloud_trace = request.headers.get("X-Cloud-Trace-Context", "")
        if cloud_trace:
            trace = cloud_trace.split("/", 1)[0]
            if trace:
                return trace
        return f"trace-{uuid.uuid4().hex[:16]}"
