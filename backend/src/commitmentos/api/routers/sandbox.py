"""The interactive judge sandbox surface (`/sandbox`).

This is the one unauthenticated mutating surface in the service, so its
isolation is structural rather than a matter of care: every handler resolves
a `SandboxWorld`, which is composed entirely of in-memory twins and holds no
credential, Firestore client, or controlled-user document. Nothing here can
name a live resource, so no request — well-formed or not — reaches one.

Three further properties keep a public surface safe to leave running:

* **No ambient authority.** The session id travels in an explicit header,
  never a cookie, so a cross-site request cannot ride an existing session
  and there is nothing for CSRF to forge.
* **Fixed inputs.** Only card ids from the deck are accepted; free text
  never reaches the model.
* **Bounded cost.** Concurrent worlds, idle lifetime, and cards per session
  are all capped, and model calls are cached per card.

Read-only seeded judge mode at `/demo` is untouched and remains
mutation-free; the two surfaces share no state or route prefix.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from commitmentos.sandbox import engine
from commitmentos.sandbox.scenario import CARDS_BY_ID
from commitmentos.sandbox.session import (
    SandboxBudgetError,
    SandboxCapacityError,
    SandboxSession,
    SandboxSessionStore,
)

SESSION_HEADER = "X-Sandbox-Session"


class ApprovalDecisionBody(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    confirmed_minutes: int | None = Field(default=None, ge=15, le=2400)


class SandboxRouter:
    def __init__(self, sessions: SandboxSessionStore) -> None:
        self._sessions = sessions

    def build(self) -> APIRouter:
        router = APIRouter(tags=["sandbox"])
        handler = self

        @router.post("/sandbox/api/session")
        async def create_session() -> JSONResponse:
            return await handler.create_session()

        @router.get("/sandbox/api/state")
        async def state(
            sandbox_session: str = Header(default="", alias=SESSION_HEADER),
        ) -> JSONResponse:
            return await handler.state(sandbox_session)

        @router.post("/sandbox/api/cards/{card_id}")
        async def play(
            card_id: str,
            sandbox_session: str = Header(default="", alias=SESSION_HEADER),
        ) -> JSONResponse:
            return await handler.play(sandbox_session, card_id)

        @router.post("/sandbox/api/approvals/{approval_id}")
        async def approve(
            approval_id: str,
            body: ApprovalDecisionBody,
            sandbox_session: str = Header(default="", alias=SESSION_HEADER),
        ) -> JSONResponse:
            return await handler.approve(sandbox_session, approval_id, body)

        @router.post("/sandbox/api/commitments/{commitment_id}/complete")
        async def complete(
            commitment_id: str,
            sandbox_session: str = Header(default="", alias=SESSION_HEADER),
        ) -> JSONResponse:
            return await handler.complete(sandbox_session, commitment_id)

        return router

    # ------------------------------------------------------------------

    def _require(self, session_id: str) -> SandboxSession:
        session = self._sessions.get(session_id) if session_id else None
        if session is None:
            # 409, not 401: nothing was wrong with the caller's identity —
            # the demonstration world simply expired or was never created.
            raise HTTPException(status_code=409, detail="sandbox session expired")
        return session

    async def create_session(self) -> JSONResponse:
        try:
            session = self._sessions.create()
        except SandboxCapacityError:
            raise HTTPException(
                status_code=503, detail="sandbox is at capacity, try again shortly"
            ) from None
        return JSONResponse(await engine.render(session), status_code=201)

    async def state(self, session_id: str) -> JSONResponse:
        return JSONResponse(await engine.render(self._require(session_id)))

    async def play(self, session_id: str, card_id: str) -> JSONResponse:
        session = self._require(session_id)
        if card_id not in CARDS_BY_ID:
            raise HTTPException(status_code=404, detail="unknown card")
        try:
            self._sessions.ensure_budget(session)
            outcome = await engine.play_card(session, card_id)
        except SandboxBudgetError:
            raise HTTPException(status_code=429, detail="session budget spent") from None
        except engine.SandboxCardError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        self._sessions.record_card(session, card_id)
        payload: dict[str, Any] = await engine.render(session)
        payload["outcome"] = {
            "cardId": outcome.card_id,
            "headline": outcome.headline,
            "detail": outcome.detail,
        }
        return JSONResponse(payload)

    async def approve(
        self, session_id: str, approval_id: str, body: ApprovalDecisionBody
    ) -> JSONResponse:
        session = self._require(session_id)
        try:
            await engine.resolve_approval(
                session, approval_id, body.decision, body.confirmed_minutes
            )
        except engine.SandboxCardError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return JSONResponse(await engine.render(session))

    async def complete(self, session_id: str, commitment_id: str) -> JSONResponse:
        session = self._require(session_id)
        try:
            await engine.complete_commitment(session, commitment_id)
        except engine.SandboxCardError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return JSONResponse(await engine.render(session))
