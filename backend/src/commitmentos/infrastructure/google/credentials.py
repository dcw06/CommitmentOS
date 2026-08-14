from __future__ import annotations

import json
import threading
from typing import Any

from google.cloud import secretmanager
from google.oauth2.credentials import Credentials


class ControlledCredentialsProvider:
    """Builds user credentials for the controlled account from Secret Manager.

    The cache is cleared on auth failure so a rotated refresh-token secret
    version is picked up without a redeploy (the Phase 0 §8 recovery finding).
    """

    def __init__(self, oauth_client_secret_ref: str, refresh_token_secret_ref: str) -> None:
        self._oauth_client_secret_ref = oauth_client_secret_ref
        self._refresh_token_secret_ref = refresh_token_secret_ref
        self._lock = threading.Lock()
        self._cached: Credentials | None = None
        self._secrets_client: Any = None

    def _access_secret(self, ref: str) -> str:
        if self._secrets_client is None:
            self._secrets_client = secretmanager.SecretManagerServiceClient()
        return self._secrets_client.access_secret_version(name=ref).payload.data.decode("utf-8")

    def credentials(self) -> Credentials:
        with self._lock:
            if self._cached is None:
                client_config = json.loads(self._access_secret(self._oauth_client_secret_ref))[
                    "web"
                ]
                self._cached = Credentials(
                    token=None,
                    refresh_token=self._access_secret(self._refresh_token_secret_ref),
                    token_uri=client_config["token_uri"],
                    client_id=client_config["client_id"],
                    client_secret=client_config["client_secret"],
                )
            return self._cached

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
