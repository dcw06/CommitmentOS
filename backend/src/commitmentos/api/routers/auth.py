from __future__ import annotations

from typing import Any

from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.application.dto import AuthenticatedActor
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.identity_verifier import IdentityVerifier
from commitmentos.application.ports.oauth_client import OAuthClient
from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.contracts.auth import OAuthTransaction


class AuthRouter:
    def __init__(
        self,
        oauth_client: OAuthClient,
        identity_verifier: IdentityVerifier,
        unit_of_work: UnitOfWork,
        csrf: CsrfProtection,
        clock: Clock,
        id_generator: IdGenerator,
        controlled_email: str,
        oauth_client_id: str,
    ) -> None:
        ...

    def build(self) -> Any:
        ...

    async def login(self, request: Any, return_to: str | None) -> Any:
        ...

    async def callback(self, request: Any, code: str, state: str) -> Any:
        ...

    async def logout(self, request: Any, actor: AuthenticatedActor) -> Any:
        ...

    async def revoke(self, request: Any, actor: AuthenticatedActor) -> Any:
        ...

    async def _create_oauth_transaction(
        self,
        return_to: str,
    ) -> tuple[OAuthTransaction, str]:
        ...

    async def _consume_oauth_transaction(self, state: str) -> OAuthTransaction:
        ...

    async def _verify_callback_identity(
        self,
        id_token: str,
        expected_nonce: str,
    ) -> None:
        ...

    def _state_hash(self, state: str) -> str:
        ...

    def _code_challenge(self, code_verifier: str) -> str:
        ...

    def _set_session_cookie(self, response: Any, raw_session_id: str) -> None:
        ...

    def _clear_session_cookie(self, response: Any) -> None:
        ...
