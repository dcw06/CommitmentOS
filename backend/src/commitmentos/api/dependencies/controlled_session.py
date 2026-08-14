from __future__ import annotations

import hashlib
from typing import Any, Mapping

from fastapi import HTTPException, Request

from commitmentos.application.dto import AuthenticatedActor
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import UnitOfWork


class ControlledSessionDependency:
    """Server-side session validation for the controlled-user application routes.

    The browser cookie is an opaque token; Firestore stores only its SHA-256.
    A valid session also stashes the session CSRF secret on `request.state`
    for the CSRF dependency to compare against.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        clock: Clock,
        controlled_user_id: str,
        session_cookie_name: str = "commitmentos_session",
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._controlled_user_id = controlled_user_id
        self._session_cookie_name = session_cookie_name

    async def __call__(self, request: Request) -> AuthenticatedActor:
        raw_session_id = request.cookies.get(self._session_cookie_name, "")
        if not raw_session_id:
            raise HTTPException(status_code=401, detail="no session")
        session_id_hash = self._session_id_hash(raw_session_id)

        async def _load(repositories: Any) -> Mapping[str, Any] | None:
            return await repositories.web_sessions.get_by_hash(session_id_hash)

        session = await self._unit_of_work.read(_load)
        if session is None:
            raise HTTPException(status_code=401, detail="unknown session")
        now = self._clock.now()
        if session.get("revoked_at") is not None:
            raise HTTPException(status_code=401, detail="session revoked")
        expires_at = session.get("expires_at")
        if expires_at is None or expires_at < now:
            raise HTTPException(status_code=401, detail="session expired")
        self._validate_identity(session)
        request.state.csrf_secret = session.get("csrf_secret", "")
        request.state.session_id_hash = session_id_hash
        return AuthenticatedActor(
            user_id=session["user_id"],
            email=session.get("email", ""),
            session_id=session_id_hash,
            authenticated_at=session.get("created_at", now),
        )

    def _session_id_hash(self, raw_session_id: str) -> str:
        return hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()

    def _validate_identity(self, session: Mapping[str, Any]) -> None:
        if session.get("user_id") != self._controlled_user_id:
            raise HTTPException(status_code=403, detail="account not allowed")
