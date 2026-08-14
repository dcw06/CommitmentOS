from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OAuthTransactionStatus(StrEnum):
    PENDING = "pending"
    CONSUMED = "consumed"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class OAuthTransaction:
    transaction_id: str
    state_hash: str
    nonce: str
    code_verifier: str
    return_to: str
    status: OAuthTransactionStatus
    revision: int
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationRequest:
    authorization_url: str
    state: str
    nonce: str
    code_challenge: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    access_token: str
    refresh_token: str | None
    id_token: str
    granted_scopes: tuple[str, ...]
    expires_at: datetime
