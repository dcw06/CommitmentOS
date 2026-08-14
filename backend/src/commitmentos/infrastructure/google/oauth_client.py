from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

from commitmentos.contracts.auth import OAuthAuthorizationRequest, OAuthTokenSet

GOOGLE_REVOKE_URI = "https://oauth2.googleapis.com/revoke"


class OAuthExchangeError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GoogleOAuthClient:
    """Authorization-code + PKCE client for the controlled-user login.

    Login requests only basic identity scopes; the mailbox/Calendar grant is
    a separate, already-stored consent. The client secret is read from the
    injected client config (Secret Manager), never from local files.
    """

    def __init__(
        self,
        client_config: Mapping[str, str],
        redirect_uri: str,
        scopes: Sequence[str],
        http_client: Any,
    ) -> None:
        self._client_config = client_config
        self._redirect_uri = redirect_uri
        self._scopes = tuple(scopes)
        self._http_client = http_client

    def create_authorization_request(
        self,
        state: str,
        nonce: str,
        code_challenge: str,
        expires_at: datetime,
    ) -> OAuthAuthorizationRequest:
        params = {
            "response_type": "code",
            "client_id": self._client_config["client_id"],
            "redirect_uri": self._redirect_uri,
            "scope": " ".join(self._scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return OAuthAuthorizationRequest(
            authorization_url=f"{self._client_config['auth_uri']}?{urlencode(params)}",
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
            expires_at=expires_at,
        )

    async def exchange_code(self, code: str, code_verifier: str) -> OAuthTokenSet:
        response = await self._http_client.post(
            self._client_config["token_uri"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._client_config["client_id"],
                "client_secret": self._client_config["client_secret"],
                "redirect_uri": self._redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        if response.status_code != 200:
            raise OAuthExchangeError("token exchange failed")
        payload = response.json()
        expires_in = int(payload.get("expires_in", 0))
        return OAuthTokenSet(
            access_token=payload.get("access_token", ""),
            refresh_token=payload.get("refresh_token"),
            id_token=payload.get("id_token", ""),
            granted_scopes=tuple(str(payload.get("scope", "")).split()),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        )

    async def revoke(self, token: str) -> None:
        await self._http_client.post(GOOGLE_REVOKE_URI, data={"token": token})

    def validate_granted_scopes(self, token_set: OAuthTokenSet) -> None:
        granted = set(token_set.granted_scopes)
        # Google reports "openid" grants as-is but expands email to the full
        # userinfo scope URI; accept either spelling for basic identity.
        aliases = {
            "email": "https://www.googleapis.com/auth/userinfo.email",
        }
        for scope in self._scopes:
            expanded = aliases.get(scope, scope)
            if scope not in granted and expanded not in granted:
                raise OAuthExchangeError(f"required scope not granted: {scope}")
