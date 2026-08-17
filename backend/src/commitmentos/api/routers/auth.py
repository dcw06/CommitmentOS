from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.application.dto import AuthenticatedActor
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.identity_verifier import IdentityVerifier
from commitmentos.application.ports.oauth_client import OAuthClient
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.contracts.auth import OAuthTransaction, OAuthTransactionStatus
from commitmentos.domain.audit.models import (
    ActivityEvent,
    ActivityEventFactory,
    ActivityEventType,
)
from commitmentos.domain.shared.errors import DomainError

TRANSACTION_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(hours=12)
SESSION_COOKIE = "commitmentos_session"
# Login may only land back on application surfaces served from this origin;
# anything else is an open-redirect vector and is rejected before any
# durable write (D4 "allowlisted redirect targets only").
ALLOWED_RETURN_TARGETS = ("/app", "/app/me", "/", "/api/v1/me")


class AuthRouter:
    """Controlled-user session issuer (plan §13.5, checklist D4).

    Ports the proven Phase 0 state/nonce/PKCE single-use transaction flow
    onto the production unit of work: the state hash is the transaction ID,
    consumption is compare-and-set, the browser cookie is opaque, and
    Firestore stores only the SHA-256 of the session token.
    """

    def __init__(
        self,
        oauth_client: OAuthClient,
        identity_verifier: IdentityVerifier,
        unit_of_work: UnitOfWork,
        session: ControlledSessionDependency,
        csrf: CsrfProtection,
        clock: Clock,
        id_generator: IdGenerator,
        controlled_user_id: str,
        controlled_email: str,
        oauth_client_id: str,
        activity_factory: ActivityEventFactory | None = None,
    ) -> None:
        self._oauth_client = oauth_client
        self._identity_verifier = identity_verifier
        self._unit_of_work = unit_of_work
        self._session = session
        self._csrf = csrf
        self._clock = clock
        self._id_generator = id_generator
        self._controlled_user_id = controlled_user_id
        self._controlled_email = controlled_email
        self._oauth_client_id = oauth_client_id
        self._activity_factory = activity_factory or ActivityEventFactory()

    def build(self) -> APIRouter:
        router = APIRouter(tags=["auth"])
        session = self._session
        csrf = self._csrf
        handler = self

        @router.get("/auth/login")
        async def login(request: Request, return_to: str | None = None) -> Response:
            return await handler.login(request, return_to)

        @router.get("/auth/callback")
        async def callback(request: Request) -> Response:
            state = request.query_params.get("state", "")
            code = request.query_params.get("code", "")
            return await handler.callback(request, code, state)

        @router.post("/auth/logout")
        async def logout(
            request: Request,
            actor: AuthenticatedActor = Depends(session),
            _csrf: None = Depends(csrf),
        ) -> Response:
            return await handler.logout(request, actor)

        @router.get("/api/v1/me")
        async def me(
            request: Request,
            actor: AuthenticatedActor = Depends(session),
        ) -> Response:
            await handler._record_access(
                "session_access",
                "allowed",
                getattr(request.state, "trace_id", "trace-unset"),
                actor.user_id,
            )
            return JSONResponse(
                {
                    "authenticated": True,
                    "user_id": actor.user_id,
                    "session": "server-side",
                    # Session-bound CSRF token echoed by mutation requests in
                    # the X-CSRF-Token header.
                    "csrf_token": getattr(request.state, "csrf_secret", ""),
                }
            )

        return router

    async def login(self, request: Request, return_to: str | None) -> Response:
        target = return_to if return_to is not None else "/app"
        if target not in ALLOWED_RETURN_TARGETS:
            raise HTTPException(status_code=400, detail="return target not allowed")
        transaction, state = await self._create_oauth_transaction(target)
        authorization = self._oauth_client.create_authorization_request(
            state=state,
            nonce=transaction.nonce,
            code_challenge=self._code_challenge(transaction.code_verifier),
            expires_at=transaction.expires_at,
        )
        return RedirectResponse(authorization.authorization_url, status_code=302)

    async def callback(self, request: Request, code: str, state: str) -> Response:
        if not state or not code:
            raise HTTPException(status_code=400, detail="missing state or code")
        transaction = await self._consume_oauth_transaction(state)
        try:
            token_set = await self._oauth_client.exchange_code(
                code, transaction.code_verifier
            )
            self._oauth_client.validate_granted_scopes(token_set)
        except Exception as error:
            await self._record_access(
                "login_callback",
                "token_exchange_rejected",
                getattr(request.state, "trace_id", "trace-unset"),
                "anonymous",
            )
            raise HTTPException(status_code=403, detail="token exchange failed") from error
        try:
            await self._verify_callback_identity(token_set.id_token, transaction.nonce)
        except HTTPException:
            await self._record_access(
                "login_callback",
                "identity_rejected",
                getattr(request.state, "trace_id", "trace-unset"),
                "anonymous",
            )
            raise

        raw_session_id = self._id_generator.new_token(32)
        now = self._clock.now()
        session_document = {
            "user_id": self._controlled_user_id,
            "email": self._controlled_email,
            "csrf_secret": self._id_generator.new_token(32),
            "created_at": now,
            "expires_at": now + SESSION_TTL,
            "revoked_at": None,
        }
        session_id_hash = hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()

        async def _create(repositories: RepositorySet) -> None:
            await repositories.web_sessions.create(
                {"session_id_hash": session_id_hash, **session_document}
            )
            await repositories.activity.append(
                self._access_event(
                    "login_callback",
                    "allowed",
                    getattr(request.state, "trace_id", "trace-unset"),
                    self._controlled_user_id,
                )
            )

        await self._unit_of_work.run(_create)
        response: Response = RedirectResponse(transaction.return_to, status_code=302)
        self._set_session_cookie(response, raw_session_id)
        return response

    async def logout(self, request: Request, actor: AuthenticatedActor) -> Response:
        session_id_hash = getattr(request.state, "session_id_hash", "")
        now = self._clock.now()

        async def _revoke(repositories: RepositorySet) -> None:
            await repositories.web_sessions.revoke(session_id_hash, now)
            await repositories.activity.append(
                self._access_event(
                    "logout",
                    "allowed",
                    getattr(request.state, "trace_id", "trace-unset"),
                    actor.user_id,
                )
            )

        await self._unit_of_work.run(_revoke)
        response: Response = JSONResponse({"authenticated": False})
        self._clear_session_cookie(response)
        return response

    async def _create_oauth_transaction(
        self,
        return_to: str,
    ) -> tuple[OAuthTransaction, str]:
        state = self._id_generator.new_token(24)
        now = self._clock.now()
        transaction = OAuthTransaction(
            transaction_id=self._state_hash(state),
            state_hash=self._state_hash(state),
            nonce=self._id_generator.new_token(16),
            code_verifier=self._id_generator.new_token(48),
            return_to=return_to,
            status=OAuthTransactionStatus.PENDING,
            revision=1,
            created_at=now,
            expires_at=now + TRANSACTION_TTL,
            consumed_at=None,
        )

        async def _create(repositories: RepositorySet) -> None:
            created = await repositories.oauth_transactions.create(transaction)
            if not created:
                raise HTTPException(status_code=500, detail="state collision")
            await repositories.activity.append(
                self._access_event(
                    "login_start",
                    "allowed",
                    f"oauth:{transaction.transaction_id}",
                    "anonymous",
                    created_at=now,
                )
            )

        await self._unit_of_work.run(_create)
        return transaction, state

    async def _consume_oauth_transaction(self, state: str) -> OAuthTransaction:
        state_hash = self._state_hash(state)
        now = self._clock.now()

        async def _consume(repositories: RepositorySet) -> OAuthTransaction:
            return await repositories.oauth_transactions.consume_pending(state_hash, now)

        try:
            return await self._unit_of_work.run(_consume)
        except DomainError as error:
            raise HTTPException(
                status_code=403, detail="unknown, expired, or replayed state"
            ) from error

    async def _verify_callback_identity(
        self,
        id_token: str,
        expected_nonce: str,
    ) -> None:
        try:
            await self._identity_verifier.verify_google_user_token(
                id_token,
                self._oauth_client_id,
                {self._controlled_email},
                expected_nonce,
            )
        except Exception as error:
            # The application allowlist: any other Google account, and any
            # nonce mismatch, gets no session.
            raise HTTPException(status_code=403, detail="account not allowed") from error

    def _state_hash(self, state: str) -> str:
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    def _code_challenge(self, code_verifier: str) -> str:
        return (
            base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode("ascii")).digest()
            )
            .decode("ascii")
            .rstrip("=")
        )

    def _set_session_cookie(self, response: Response, raw_session_id: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            raw_session_id,
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )

    def _clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    async def _record_access(
        self,
        operation: str,
        outcome: str,
        trace_id: str,
        actor: str,
    ) -> None:
        async def _record(repositories: RepositorySet) -> None:
            await repositories.activity.append(
                self._access_event(operation, outcome, trace_id, actor)
            )

        await self._unit_of_work.run(_record)

    def _access_event(
        self,
        operation: str,
        outcome: str,
        trace_id: str,
        actor: str,
        *,
        created_at: datetime | None = None,
    ) -> ActivityEvent:
        return self._activity_factory.create(
            user_id=self._controlled_user_id,
            event_type=ActivityEventType.ACCESS_RECORDED,
            trace_id=trace_id,
            actor=actor,
            summary=f"Access {operation}: {outcome}",
            payload={"operation": operation, "outcome": outcome},
            created_at=created_at or self._clock.now(),
        )
