from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    subject: str
    email: str | None
    audience: str
    issuer: str


class IdentityVerifier(Protocol):
    async def verify_oidc_token(
        self,
        token: str,
        expected_audience: str,
        allowed_subjects: set[str],
    ) -> VerifiedIdentity:
        ...

    async def verify_google_user_token(
        self,
        token: str,
        expected_audience: str,
        allowed_emails: set[str],
        expected_nonce: str | None,
    ) -> VerifiedIdentity:
        ...
