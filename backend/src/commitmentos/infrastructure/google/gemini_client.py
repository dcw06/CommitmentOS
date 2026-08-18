"""Lazy Gemini client factories with deliberately narrow authority.

Each instance retains one Secret Manager resource name and nothing from the
application composition root. In particular, the public sandbox receives a
factory for its own API key, so it cannot retain or traverse the production
container, controlled-user credentials, Firestore adapters, or production
Gemini client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SecretManagerGeminiClientFactory:
    secret_ref: str
    request_timeout_seconds: float = 45

    def __call__(self) -> Any:
        from google import genai
        from google.cloud import secretmanager
        from google.genai import types

        api_key = (
            secretmanager.SecretManagerServiceClient()
            .access_secret_version(name=self.secret_ref)
            .payload.data.decode("utf-8")
        )
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(self.request_timeout_seconds * 1000),
                # The adapter owns its one narrow thinking-config fallback;
                # hidden transport retries would violate the call budget.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
