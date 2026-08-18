"""Construct non-overlapping model capabilities for live and public traffic."""

from __future__ import annotations

from dataclasses import dataclass

from commitmentos.bootstrap.settings import Settings
from commitmentos.infrastructure.google.gemini_client import (
    SecretManagerGeminiClientFactory,
)
from commitmentos.infrastructure.google.gemini_interpreter import GeminiInterpreter


@dataclass(frozen=True, slots=True)
class GeminiInterpreterBoundary:
    controlled_data: GeminiInterpreter
    sandbox: GeminiInterpreter | None


def build_gemini_interpreters(settings: Settings) -> GeminiInterpreterBoundary:
    """Build distinct interpreter/client/key graphs for the two trust zones."""

    def interpreter(
        secret_ref: str,
        display_name: str,
        request_timeout_seconds: float,
    ) -> GeminiInterpreter:
        return GeminiInterpreter(
            client_factory=SecretManagerGeminiClientFactory(
                secret_ref, request_timeout_seconds
            ),
            model_id=settings.gemini_model_id,
            prompt_version=settings.prompt_version,
            schema_version=settings.extraction_schema_version,
            thinking_level=settings.gemini_thinking_level,
            controlled_display_name=display_name,
            request_timeout_seconds=request_timeout_seconds,
        )

    sandbox_secret_ref = settings.sandbox_gemini_api_key_secret_ref
    return GeminiInterpreterBoundary(
        controlled_data=interpreter(
            settings.gemini_api_key_secret_ref,
            "Controlled User",
            settings.gemini_request_timeout_seconds,
        ),
        sandbox=(
            interpreter(
                sandbox_secret_ref,
                "Sandbox User",
                settings.sandbox_gemini_request_timeout_seconds,
            )
            if sandbox_secret_ref
            else None
        ),
    )
