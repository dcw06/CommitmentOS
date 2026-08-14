from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from commitmentos.application.ports.identity_verifier import IdentityVerifier


@dataclass(frozen=True, slots=True)
class GoogleDeliveryPrincipal:
    subject: str
    email: str
    audience: str
    route_kind: str


class GoogleOidcDependency:
    """Route-group trust contract for Pub/Sub, Cloud Tasks, and Scheduler.

    Verification happens before any Firestore write, task dispatch, or Google
    API call; a wrong audience or identity is rejected with zero side effects.
    """

    def __init__(
        self,
        identity_verifier: IdentityVerifier,
        expected_audience: str,
        expected_email: str,
        route_kind: str,
    ) -> None:
        self._identity_verifier = identity_verifier
        self._expected_audience = expected_audience
        self._expected_email = expected_email
        self._route_kind = route_kind

    async def __call__(self, request: Request) -> GoogleDeliveryPrincipal:
        token = self._bearer_token(request)
        try:
            identity = await self._identity_verifier.verify_oidc_token(
                token, self._expected_audience, {self._expected_email}
            )
        except Exception as error:
            raise HTTPException(status_code=403, detail="invalid delivery token") from error
        return GoogleDeliveryPrincipal(
            subject=identity.subject,
            email=identity.email or "",
            audience=identity.audience,
            route_kind=self._route_kind,
        )

    def _bearer_token(self, request: Any) -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        return header[7:]
