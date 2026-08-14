from __future__ import annotations

import asyncio

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from commitmentos.application.ports.identity_verifier import VerifiedIdentity


class IdentityVerificationError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GoogleIdentityVerifier:
    """Verifies Google-signed OIDC tokens for trusted delivery routes."""

    def __init__(self) -> None:
        self._request = google_requests.Request()

    async def verify_oidc_token(
        self,
        token: str,
        expected_audience: str,
        allowed_subjects: set[str],
    ) -> VerifiedIdentity:
        claims = await self._verify(token, expected_audience)
        email = claims.get("email")
        if email not in allowed_subjects or not claims.get("email_verified"):
            raise IdentityVerificationError("unexpected delivery identity")
        return VerifiedIdentity(
            subject=claims.get("sub", ""),
            email=email,
            audience=claims.get("aud", ""),
            issuer=claims.get("iss", ""),
        )

    async def verify_google_user_token(
        self,
        token: str,
        expected_audience: str,
        allowed_emails: set[str],
        expected_nonce: str | None,
    ) -> VerifiedIdentity:
        claims = await self._verify(token, expected_audience)
        email = claims.get("email")
        if email not in allowed_emails or not claims.get("email_verified"):
            raise IdentityVerificationError("account not allowed")
        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            raise IdentityVerificationError("nonce mismatch")
        return VerifiedIdentity(
            subject=claims.get("sub", ""),
            email=email,
            audience=claims.get("aud", ""),
            issuer=claims.get("iss", ""),
        )

    async def _verify(self, token: str, expected_audience: str) -> dict:
        def _blocking() -> dict:
            return google_id_token.verify_oauth2_token(
                token, self._request, audience=expected_audience
            )

        try:
            return await asyncio.to_thread(_blocking)
        except Exception as error:
            raise IdentityVerificationError("invalid token") from error
