from __future__ import annotations

import hmac

from fastapi import HTTPException, Request


class CsrfProtection:
    """Session-bound CSRF validation for every controlled mutation route.

    The session's CSRF secret (returned once through the authenticated
    `/api/v1/me` read) must be echoed in the request header. Comparison is
    constant-time; a missing or wrong token is rejected before any command
    runs, so zero durable side effects are possible.
    """

    def __init__(self, header_name: str = "X-CSRF-Token") -> None:
        self._header_name = header_name

    async def __call__(self, request: Request) -> None:
        session_secret = getattr(request.state, "csrf_secret", "")
        header_token = request.headers.get(self._header_name, "")
        self.validate(session_secret, header_token)

    def validate(self, session_secret: str, header_token: str) -> None:
        if not session_secret or not header_token:
            raise HTTPException(status_code=403, detail="missing CSRF token")
        if not hmac.compare_digest(session_secret, header_token):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
