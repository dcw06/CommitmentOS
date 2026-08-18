from __future__ import annotations

from commitmentos.bootstrap.gemini_boundary import build_gemini_interpreters
from commitmentos.bootstrap.settings import Settings
from commitmentos.infrastructure.google.gemini_client import (
    SecretManagerGeminiClientFactory,
)


def test_public_sandbox_has_a_detached_client_key_and_interpreter() -> None:
    settings = Settings.model_construct(
        gemini_api_key_secret_ref="projects/test/secrets/controlled/versions/1",
        sandbox_gemini_api_key_secret_ref="projects/test/secrets/sandbox/versions/1",
        gemini_model_id="fixture-model",
        prompt_version="commitment_interpretation_v2",
        extraction_schema_version="extraction_v2",
        gemini_thinking_level="low",
    )

    boundary = build_gemini_interpreters(settings)

    assert boundary.sandbox is not None
    assert boundary.controlled_data is not boundary.sandbox
    controlled_factory = boundary.controlled_data._client_factory
    sandbox_factory = boundary.sandbox._client_factory
    assert isinstance(controlled_factory, SecretManagerGeminiClientFactory)
    assert isinstance(sandbox_factory, SecretManagerGeminiClientFactory)
    assert controlled_factory is not sandbox_factory
    assert controlled_factory.secret_ref != sandbox_factory.secret_ref
    assert not hasattr(sandbox_factory, "__self__")
    assert not hasattr(sandbox_factory, "__dict__")


def test_development_can_disable_the_live_sandbox_edge() -> None:
    settings = Settings.model_construct(
        gemini_api_key_secret_ref="projects/test/secrets/controlled/versions/1",
        sandbox_gemini_api_key_secret_ref=None,
        gemini_model_id="fixture-model",
        prompt_version="commitment_interpretation_v2",
        extraction_schema_version="extraction_v2",
        gemini_thinking_level="low",
    )

    boundary = build_gemini_interpreters(settings)

    assert boundary.sandbox is None
