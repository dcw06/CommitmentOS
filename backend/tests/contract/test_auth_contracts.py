"""D4 session negative matrix — the AuthRouter as the production session issuer.

Checklist Part II D4: allowlisted redirect targets only; missing, mismatched,
expired, and replayed state; mismatched nonce; callback replay cannot create
a second session; logout revokes; expiry and revocation enforced. Every
rejection must leave zero durable side effects.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeClock, InMemoryUnitOfWork, SequentialIdGenerator
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.api.middleware.request_context import RequestContextMiddleware
from commitmentos.api.routers.auth import AuthRouter
from commitmentos.contracts.auth import OAuthAuthorizationRequest, OAuthTokenSet

CONTROLLED_USER = "user_fixture_controlled_001"
CONTROLLED_EMAIL = "controlled@example.invalid"
OTHER_EMAIL = "attacker@example.invalid"
CLIENT_ID = "client-id-fixture"


class OAuthExchangeError(Exception):
    pass


class FakeOAuthClient:
    """Scripted provider: code 'valid:<email>' exchanges into an id token
    carrying that email and the transaction nonce supplied at login."""

    def __init__(self) -> None:
        self.issued_nonces: list[str] = []
        self.override_nonce: str | None = None
        self.revoked: list[str] = []

    def create_authorization_request(
        self,
        state: str,
        nonce: str,
        code_challenge: str,
        expires_at: datetime,
    ) -> OAuthAuthorizationRequest:
        self.issued_nonces.append(nonce)
        return OAuthAuthorizationRequest(
            authorization_url=(
                f"https://accounts.google.example/auth?state={state}&nonce={nonce}"
            ),
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
            expires_at=expires_at,
        )

    async def exchange_code(self, code: str, code_verifier: str) -> OAuthTokenSet:
        if not code.startswith("valid:"):
            raise OAuthExchangeError("token exchange failed")
        email = code.split(":", 1)[1]
        nonce = (
            self.override_nonce
            if self.override_nonce is not None
            else (self.issued_nonces[-1] if self.issued_nonces else "")
        )
        return OAuthTokenSet(
            access_token="access-token",
            refresh_token=None,
            id_token=f"idtok:{email}:{nonce}",
            granted_scopes=("openid", "email"),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    async def revoke(self, token: str) -> None:
        self.revoked.append(token)

    def validate_granted_scopes(self, token_set: OAuthTokenSet) -> None:
        return None


class IdentityVerificationError(Exception):
    pass


class FakeUserIdentityVerifier:
    async def verify_oidc_token(self, token, expected_audience, allowed_subjects):
        raise NotImplementedError

    async def verify_google_user_token(
        self,
        token: str,
        expected_audience: str,
        allowed_emails: set[str],
        expected_nonce: str | None,
    ):
        if expected_audience != CLIENT_ID or not token.startswith("idtok:"):
            raise IdentityVerificationError("invalid token")
        _, email, nonce = token.split(":", 2)
        if email not in allowed_emails:
            raise IdentityVerificationError("account not allowed")
        if expected_nonce is not None and nonce != expected_nonce:
            raise IdentityVerificationError("nonce mismatch")
        return None


@dataclass
class AuthHarness:
    client: TestClient
    store: dict
    clock: FakeClock
    oauth: FakeOAuthClient

    def login(self, return_to: str | None = "/app") -> str:
        """Start a login and return the state Google would echo back."""
        params = {} if return_to is None else {"return_to": return_to}
        response = self.client.get(
            "/auth/login", params=params, follow_redirects=False
        )
        assert response.status_code == 302
        location = response.headers["location"]
        return location.split("state=", 1)[1].split("&", 1)[0]

    def callback(self, state: str, code: str = f"valid:{CONTROLLED_EMAIL}"):
        return self.client.get(
            "/auth/callback",
            params={"state": state, "code": code},
            follow_redirects=False,
        )

    def complete_login(self) -> str:
        state = self.login()
        response = self.callback(state)
        assert response.status_code == 302
        return response.cookies["commitmentos_session"]

    def me(self, session_token: str | None = None):
        cookies = (
            {"commitmentos_session": session_token} if session_token is not None else None
        )
        return self.client.get("/api/v1/me", cookies=cookies)


@pytest.fixture
def auth() -> AuthHarness:
    store: dict = {}
    clock = FakeClock()
    unit_of_work = InMemoryUnitOfWork(store, clock)
    oauth = FakeOAuthClient()
    session = ControlledSessionDependency(unit_of_work, clock, CONTROLLED_USER)
    csrf = CsrfProtection()
    router = AuthRouter(
        oauth,
        FakeUserIdentityVerifier(),
        unit_of_work,
        session,
        csrf,
        clock,
        SequentialIdGenerator(),
        CONTROLLED_USER,
        CONTROLLED_EMAIL,
        CLIENT_ID,
    )
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router.build())
    client = TestClient(app, base_url="https://testserver")
    return AuthHarness(client=client, store=store, clock=clock, oauth=oauth)


class TestLoginRedirectAllowlist:
    def test_login_starts_only_through_allowlisted_targets(self, auth: AuthHarness) -> None:
        for target in ("https://evil.example", "//evil.example", "/etc/passwd", ""):
            response = auth.client.get(
                "/auth/login", params={"return_to": target}, follow_redirects=False
            )
            assert response.status_code == 400
        assert auth.store.get("oauth_transactions", {}) == {}

    def test_allowlisted_target_creates_one_pending_transaction(
        self, auth: AuthHarness
    ) -> None:
        state = auth.login("/app")
        assert len(auth.store["oauth_transactions"]) == 1
        stored = next(iter(auth.store["oauth_transactions"].values()))
        assert stored["status"] == "pending"
        assert stored["return_to"] == "/app"
        # The state itself is never stored — only its hash keys the document.
        assert state not in auth.store["oauth_transactions"]


class TestCallbackStateMatrix:
    def test_missing_state_or_code_is_rejected(self, auth: AuthHarness) -> None:
        assert auth.client.get(
            "/auth/callback", params={"code": "valid:x"}, follow_redirects=False
        ).status_code == 400
        assert auth.client.get(
            "/auth/callback", params={"state": "abc"}, follow_redirects=False
        ).status_code == 400
        assert auth.store.get("web_sessions", {}) == {}

    def test_mismatched_state_is_rejected_with_zero_side_effects(
        self, auth: AuthHarness
    ) -> None:
        auth.login()
        before = copy.deepcopy(auth.store)
        response = auth.callback("never-issued-state")
        assert response.status_code == 403
        assert auth.store == before
        assert auth.store.get("web_sessions", {}) == {}

    def test_expired_state_is_rejected(self, auth: AuthHarness) -> None:
        state = auth.login()
        auth.clock.advance(11 * 60)
        response = auth.callback(state)
        assert response.status_code == 403
        assert auth.store.get("web_sessions", {}) == {}
        stored = next(iter(auth.store["oauth_transactions"].values()))
        assert stored["status"] == "pending"

    def test_replayed_state_cannot_create_a_second_session(
        self, auth: AuthHarness
    ) -> None:
        state = auth.login()
        first = auth.callback(state)
        assert first.status_code == 302
        assert len(auth.store["web_sessions"]) == 1
        replay = auth.callback(state)
        assert replay.status_code == 403
        assert len(auth.store["web_sessions"]) == 1

    def test_mismatched_nonce_is_rejected_without_a_session(
        self, auth: AuthHarness
    ) -> None:
        state = auth.login()
        auth.oauth.override_nonce = "attacker-substituted-nonce"
        response = auth.callback(state)
        assert response.status_code == 403
        assert auth.store.get("web_sessions", {}) == {}

    def test_non_allowlisted_account_gets_no_session(self, auth: AuthHarness) -> None:
        state = auth.login()
        response = auth.callback(state, code=f"valid:{OTHER_EMAIL}")
        assert response.status_code == 403
        assert auth.store.get("web_sessions", {}) == {}

    def test_failed_exchange_gets_no_session(self, auth: AuthHarness) -> None:
        state = auth.login()
        response = auth.callback(state, code="invalid-code")
        assert response.status_code == 403
        assert auth.store.get("web_sessions", {}) == {}


class TestSessionLifecycle:
    def test_successful_login_issues_opaque_server_side_session(
        self, auth: AuthHarness
    ) -> None:
        token = auth.complete_login()
        # Firestore stores only the SHA-256 of the cookie value.
        assert token not in auth.store["web_sessions"]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        stored = auth.store["web_sessions"][token_hash]
        assert stored["user_id"] == CONTROLLED_USER
        assert stored["revoked_at"] is None
        response = auth.me(token)
        assert response.status_code == 200
        body = response.json()
        assert body["authenticated"] is True
        assert body["csrf_token"] == stored["csrf_secret"]
        access = [
            row["payload"]
            for row in auth.store["activity_events"].values()
            if row["event_type"] == "access_recorded"
        ]
        assert {row["operation"] for row in access} == {
            "login_start",
            "login_callback",
            "session_access",
        }
        assert all("email" not in row for row in access)

    def test_session_cookie_flags(self, auth: AuthHarness) -> None:
        state = auth.login()
        response = auth.callback(state)
        cookie_header = response.headers["set-cookie"]
        assert "HttpOnly" in cookie_header
        assert "Secure" in cookie_header
        assert "SameSite=lax" in cookie_header.replace("samesite", "SameSite")
        # The cookie carries only the opaque token, never OAuth material.
        assert "access-token" not in cookie_header
        assert "idtok" not in cookie_header

    def test_logout_revokes_the_current_session(self, auth: AuthHarness) -> None:
        token = auth.complete_login()
        csrf_token = auth.me(token).json()["csrf_token"]
        response = auth.client.post(
            "/auth/logout",
            cookies={"commitmentos_session": token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200
        assert auth.me(token).status_code == 401
        assert any(
            row["payload"] == {"operation": "logout", "outcome": "allowed"}
            for row in auth.store["activity_events"].values()
        )

    def test_logout_requires_csrf(self, auth: AuthHarness) -> None:
        token = auth.complete_login()
        response = auth.client.post(
            "/auth/logout", cookies={"commitmentos_session": token}
        )
        assert response.status_code == 403
        # The session survives the rejected logout.
        assert auth.me(token).status_code == 200

    def test_session_expiry_is_enforced(self, auth: AuthHarness) -> None:
        token = auth.complete_login()
        auth.clock.advance(13 * 60 * 60)
        assert auth.me(token).status_code == 401

    def test_revoked_session_is_rejected(self, auth: AuthHarness) -> None:
        token = auth.complete_login()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        auth.store["web_sessions"][token_hash]["revoked_at"] = auth.clock.now()
        assert auth.me(token).status_code == 401

    def test_unknown_and_missing_session_rejected(self, auth: AuthHarness) -> None:
        assert auth.me("never-issued-token").status_code == 401
        assert auth.me(None).status_code == 401
