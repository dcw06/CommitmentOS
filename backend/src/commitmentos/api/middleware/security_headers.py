from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from starlette.middleware.base import BaseHTTPMiddleware

DEFAULT_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(app)
        self._headers = dict(headers or DEFAULT_SECURITY_HEADERS)

    async def dispatch(
        self,
        request: Any,
        call_next: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        response = await call_next(request)
        return self._apply_headers(response)

    def _apply_headers(self, response: Any) -> Any:
        for name, value in self._headers.items():
            response.headers.setdefault(name, value)
        return response
