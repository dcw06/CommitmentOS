from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from commitmentos.contracts.auth import OAuthAuthorizationRequest, OAuthTokenSet


class GoogleOAuthClient:
    def __init__(
        self,
        client_config: Mapping[str, str],
        redirect_uri: str,
        scopes: Sequence[str],
        http_client: Any,
    ) -> None:
        ...

    def create_authorization_request(
        self,
        state: str,
        nonce: str,
        code_challenge: str,
        expires_at: datetime,
    ) -> OAuthAuthorizationRequest:
        ...

    async def exchange_code(
        self,
        code: str,
        code_verifier: str,
    ) -> OAuthTokenSet:
        ...

    async def revoke(self, token: str) -> None:
        ...

    def validate_granted_scopes(self, token_set: OAuthTokenSet) -> None:
        ...
