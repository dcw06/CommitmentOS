"""Phase 0 Section 9 spike routes — controlled-user session and seeded demo.

Minimal-depth trust contracts:

- GET /auth/login      — one-time state/nonce/PKCE transaction, redirect to Google
- GET /auth/callback   — single-use transaction, id_token verification, the
                         application allowlist, opaque server-side session
- GET /app/me          — authenticated read over the session cookie
- GET /demo/today      — repository-seeded read-only data
- POST /demo/*         — always rejected, zero side effects

Login requests only basic identity scopes (openid, email); mailbox consent is
a separate, already-stored grant. The full negative matrix (expiry, logout,
CSRF suite) is Phase 5 per Part II.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from google.auth.transport import requests as google_requests
from google.cloud import firestore
from google.oauth2 import id_token as google_id_token

from commitmentos.bootstrap.settings import Settings
from commitmentos.spike.section4_gmail import _access_secret, _firestore_client

LOGIN_SCOPES = "openid email"
TRANSACTION_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(hours=12)
SESSION_COOKIE = "commitmentos_session"
DEMO_DATA_DIR = Path(__file__).resolve().parents[1] / "demo_data"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _client_config(settings: Settings) -> dict:
    return json.loads(_access_secret(settings.oauth_client_secret_ref))["web"]


def build_auth_router(settings: Settings) -> APIRouter:
    router = APIRouter()
    project = settings.google_cloud_project

    def _transactions() -> firestore.CollectionReference:
        return _firestore_client(project).collection("oauth_transactions")

    def _sessions() -> firestore.CollectionReference:
        return _firestore_client(project).collection("web_sessions")

    @router.get("/auth/login")
    async def login() -> RedirectResponse:
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        now = datetime.now(timezone.utc)
        _transactions().document(_sha256(state)).set(
            {
                "nonce_hash": _sha256(nonce),
                "pkce_verifier": verifier,
                "created_at": now,
                "expires_at": now + TRANSACTION_TTL,
                "used": False,
            }
        )
        config = _client_config(settings)
        params = {
            "response_type": "code",
            "client_id": config["client_id"],
            "redirect_uri": str(settings.oauth_redirect_uri),
            "scope": LOGIN_SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return RedirectResponse(f"{config['auth_uri']}?{urlencode(params)}", status_code=302)

    @router.get("/auth/callback")
    async def callback(request: Request) -> Response:
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        if not state or not code:
            raise HTTPException(status_code=400, detail="missing state or code")

        client = _firestore_client(project)
        transaction_ref = _transactions().document(_sha256(state))
        firestore_txn = client.transaction()

        @firestore.transactional
        def _consume(txn: firestore.Transaction) -> dict:
            snapshot = transaction_ref.get(transaction=txn)
            if not snapshot.exists:
                raise HTTPException(status_code=403, detail="unknown or replayed state")
            data = snapshot.to_dict()
            if data["used"]:
                raise HTTPException(status_code=403, detail="unknown or replayed state")
            if data["expires_at"] < datetime.now(timezone.utc):
                raise HTTPException(status_code=403, detail="expired login transaction")
            txn.update(transaction_ref, {"used": True, "used_at": datetime.now(timezone.utc)})
            return data

        transaction = _consume(firestore_txn)

        config = _client_config(settings)
        token_response = httpx.post(
            config["token_uri"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "redirect_uri": str(settings.oauth_redirect_uri),
                "code_verifier": transaction["pkce_verifier"],
            },
            timeout=30,
        )
        if token_response.status_code != 200:
            raise HTTPException(status_code=403, detail="token exchange failed")
        raw_id_token = token_response.json().get("id_token", "")
        try:
            claims = google_id_token.verify_oauth2_token(
                raw_id_token, google_requests.Request(), audience=config["client_id"]
            )
        except Exception as error:
            raise HTTPException(status_code=403, detail="invalid id token") from error
        if _sha256(claims.get("nonce", "")) != transaction["nonce_hash"]:
            raise HTTPException(status_code=403, detail="nonce mismatch")

        email = claims.get("email", "")
        if not claims.get("email_verified") or email != settings.controlled_email:
            # The application allowlist: any other Google account gets no session.
            raise HTTPException(status_code=403, detail="account not allowed")

        session_token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        _sessions().document(_sha256(session_token)).set(
            {
                "user_id": settings.controlled_user_id,
                "email": settings.controlled_email,
                "csrf_secret": secrets.token_urlsafe(32),
                "created_at": now,
                "expires_at": now + SESSION_TTL,
                "revoked_at": None,
            }
        )
        response = RedirectResponse("/app/me", status_code=302)
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/app/me")
    async def me(request: Request) -> JSONResponse:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            raise HTTPException(status_code=401, detail="no session")
        snapshot = _sessions().document(_sha256(token)).get()
        if not snapshot.exists:
            raise HTTPException(status_code=401, detail="unknown session")
        session = snapshot.to_dict()
        if session.get("revoked_at") or session["expires_at"] < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="session expired")
        return JSONResponse(
            {
                "authenticated": True,
                "user_id": session["user_id"],
                "session": "server-side",
                # Session-bound CSRF token echoed by mutation requests in
                # the X-CSRF-Token header.
                "csrf_token": session.get("csrf_secret", ""),
            }
        )

    @router.get("/demo/today")
    async def demo_today() -> JSONResponse:
        return JSONResponse(json.loads((DEMO_DATA_DIR / "today.json").read_text()))

    @router.post("/demo/{rest:path}")
    async def demo_mutation(rest: str) -> Response:
        raise HTTPException(status_code=403, detail="demo mode is read-only")

    return router
