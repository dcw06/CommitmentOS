"""The interactive judge sandbox surface (`/sandbox`).

This is the one unauthenticated mutating surface in the service, so its
isolation is structural rather than a matter of care: every handler resolves
a `SandboxWorld`, which is composed entirely of in-memory twins. The only
external capability is a sandbox-only Gemini adapter with a separate key and
no tools; no Firestore client, controlled-user credential/document, or
production interpreter is reachable from it.

Three further properties keep a public surface safe to leave running:

* **No ambient authority.** The session id travels in an explicit header,
  never a cookie, so a cross-site request cannot ride an existing session
  and there is nothing for CSRF to forge.
* **Constrained free play.** A judge may send bounded text only as one of the
  two simulated participants. It reaches a sandbox-only model adapter with no
  mutation tools; recorded card answers are never substituted.
* **Bounded cost.** World creation, concurrent worlds, idle/absolute lifetime,
  cards, custom messages, all model methods, model concurrency, and the guided
  cache are capped. Read-only polling cannot keep a world alive.

Read-only seeded judge mode at `/demo` is untouched and remains
mutation-free; the two surfaces share no state or route prefix.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from commitmentos.sandbox import engine
from commitmentos.sandbox.scenario import CARDS_BY_ID
from commitmentos.sandbox.session import (
    SandboxBudgetError,
    SandboxCapacityError,
    SandboxCustomMessageBudgetError,
    SandboxCustomMessageRateError,
    SandboxInterpretationRetryError,
    SandboxMode,
    SandboxModeError,
    SandboxSession,
    SandboxSessionRateError,
    SandboxSessionStore,
)

SESSION_HEADER = "X-Sandbox-Session"


class ApprovalDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(approve|reject)$")
    confirmed_minutes: int | None = Field(
        default=None,
        ge=30,
        le=2400,
        multiple_of=15,
    )
    deadline: datetime | None = None
    ownership_type: Literal[
        "my_commitment", "request_to_me", "commitment_to_me"
    ] | None = None
    choice: str | None = Field(default=None, min_length=1, max_length=100)
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("choice", "reason")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("value must contain text")
        if "\x00" in value:
            raise ValueError("value contains an unsupported character")
        return value


class CustomMessageBody(BaseModel):
    sender: Literal["you", "jordan"]
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must contain text")
        if "\x00" in value:
            raise ValueError("message contains an unsupported character")
        return value


class SandboxModeBody(BaseModel):
    mode: Literal["guided", "free_play"]
    subject: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_subject(self) -> SandboxModeBody:
        if self.subject is not None:
            self.subject = self.subject.strip()
        if self.mode == "free_play" and not self.subject:
            raise ValueError("free-play mode requires a subject")
        if self.subject is not None and "\x00" in self.subject:
            raise ValueError("subject contains an unsupported character")
        return self


class SandboxRouter:
    def __init__(self, sessions: SandboxSessionStore) -> None:
        self._sessions = sessions

    def build(self) -> APIRouter:
        router = APIRouter(tags=["sandbox"])
        handler = self

        @router.post("/sandbox/api/session")
        async def create_session() -> JSONResponse:
            return await handler.create_session()

        @router.post("/sandbox/api/session/reset")
        async def reset_session(
            sandbox_session: str = Header(default="", alias=SESSION_HEADER),
        ) -> JSONResponse:
            return await handler.reset_session(sandbox_session)

        @router.post("/sandbox/api/mode")
        async def choose_mode(
            body: SandboxModeBody,
            sandbox_session: str = Header(default="", alias=SESSION_HEADER),
        ) -> JSONResponse:
            return await handler.choose_mode(sandbox_session, body)

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

        @router.post("/sandbox/api/messages")
        async def send_message(
            body: CustomMessageBody,
            sandbox_session: str = Header(default="", alias=SESSION_HEADER),
        ) -> JSONResponse:
            return await handler.send_message(sandbox_session, body)

        @router.post("/sandbox/api/messages/{message_id}/retry")
        async def retry_message(
            message_id: str,
            sandbox_session: str = Header(default="", alias=SESSION_HEADER),
        ) -> JSONResponse:
            return await handler.retry_message(sandbox_session, message_id)

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

    def _ensure_current(self, session_id: str, session: SandboxSession) -> None:
        if not self._sessions.is_current(session_id, session):
            raise HTTPException(status_code=409, detail="sandbox session expired")

    async def create_session(self) -> JSONResponse:
        try:
            session = self._sessions.create()
        except SandboxSessionRateError:
            raise HTTPException(
                status_code=429,
                detail="sandbox session creation rate reached; retry in one minute",
                headers={"Retry-After": "60"},
            ) from None
        except SandboxCapacityError:
            raise HTTPException(
                status_code=503, detail="sandbox is at capacity, try again shortly"
            ) from None
        return JSONResponse(await engine.render(session), status_code=201)

    async def reset_session(self, session_id: str) -> JSONResponse:
        session = self._require(session_id)
        async with session.lock:
            self._ensure_current(session_id, session)
            try:
                replacement = self._sessions.reset(session_id)
            except KeyError:
                raise HTTPException(
                    status_code=409, detail="sandbox session expired"
                ) from None
        return JSONResponse(await engine.render(replacement), status_code=201)

    async def choose_mode(
        self, session_id: str, body: SandboxModeBody
    ) -> JSONResponse:
        session = self._require(session_id)
        async with session.lock:
            self._ensure_current(session_id, session)
            try:
                self._sessions.select_mode(
                    session,
                    SandboxMode(body.mode),
                    subject=body.subject,
                )
            except SandboxModeError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            return JSONResponse(await engine.render(session))

    async def state(self, session_id: str) -> JSONResponse:
        session = self._require(session_id)
        async with session.lock:
            self._ensure_current(session_id, session)
            return JSONResponse(await engine.render(session))

    async def play(self, session_id: str, card_id: str) -> JSONResponse:
        session = self._require(session_id)
        if card_id not in CARDS_BY_ID:
            raise HTTPException(status_code=404, detail="unknown card")
        async with session.lock:
            self._ensure_current(session_id, session)
            try:
                card = CARDS_BY_ID[card_id]
                if card.kind == "message":
                    self._sessions.require_mode(session, SandboxMode.GUIDED)
                elif session.mode is SandboxMode.UNSELECTED:
                    self._sessions.require_mode(session, SandboxMode.GUIDED)
                self._sessions.ensure_budget(session)
                outcome = await engine.play_card(session, card_id)
            except SandboxModeError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            except SandboxBudgetError:
                raise HTTPException(
                    status_code=429, detail="session budget spent"
                ) from None
            except engine.SandboxCardError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            self._sessions.record_card(session, card_id, outcome.detail)
            payload: dict[str, Any] = await engine.render(session)
            payload["outcome"] = {
                "cardId": outcome.card_id,
                "headline": outcome.headline,
                "detail": outcome.detail,
            }
            return JSONResponse(payload)

    async def send_message(
        self, session_id: str, body: CustomMessageBody
    ) -> JSONResponse:
        session = self._require(session_id)
        async with session.lock:
            self._ensure_current(session_id, session)
            try:
                self._sessions.require_mode(session, SandboxMode.FREE_PLAY)
                message_id = self._sessions.charge_custom_message(session)
                outcome = await engine.play_custom_message(
                    session, message_id, body.sender, body.message
                )
            except SandboxModeError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            except SandboxCustomMessageBudgetError:
                raise HTTPException(
                    status_code=429,
                    detail="custom message limit reached; start over to try more",
                ) from None
            except SandboxCustomMessageRateError:
                raise HTTPException(
                    status_code=429,
                    detail="custom message rate limit reached; try again shortly",
                ) from None
            except engine.SandboxCardError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            self._sessions.record_custom_message(
                session,
                message_id,
                body.sender,
                body.message,
                outcome.detail,
            )
            payload: dict[str, Any] = await engine.render(session)
            payload["outcome"] = {
                "cardId": outcome.card_id,
                "headline": outcome.headline,
                "detail": outcome.detail,
            }
            return JSONResponse(payload)

    async def retry_message(
        self,
        session_id: str,
        message_id: str,
    ) -> JSONResponse:
        session = self._require(session_id)
        async with session.lock:
            self._ensure_current(session_id, session)
            try:
                self._sessions.require_mode(session, SandboxMode.FREE_PLAY)
                attempt = self._sessions.charge_interpretation_retry(
                    session,
                    message_id,
                )
                outcome = await engine.retry_custom_message(
                    session,
                    message_id,
                    attempt,
                )
            except SandboxModeError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            except SandboxInterpretationRetryError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            except SandboxCustomMessageRateError:
                raise HTTPException(
                    status_code=429,
                    detail="interpretation retry rate limit reached; try again shortly",
                ) from None
            except engine.SandboxCardError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            self._sessions.record_interpretation_retry(
                session,
                message_id,
                outcome.detail,
            )
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
        async with session.lock:
            self._ensure_current(session_id, session)
            try:
                await engine.resolve_approval(
                    session,
                    approval_id,
                    body.model_dump(exclude_none=True),
                )
            except engine.SandboxCardError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            self._sessions.touch(session)
            return JSONResponse(await engine.render(session))

    async def complete(self, session_id: str, commitment_id: str) -> JSONResponse:
        session = self._require(session_id)
        async with session.lock:
            self._ensure_current(session_id, session)
            try:
                await engine.complete_commitment(session, commitment_id)
            except engine.SandboxCardError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            self._sessions.touch(session)
            return JSONResponse(await engine.render(session))
