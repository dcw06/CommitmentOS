from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from google import genai
from google.cloud import secretmanager

from commitmentos.infrastructure.google.gemini_client import (
    SecretManagerGeminiClientFactory,
)
from commitmentos.infrastructure.google.gemini_interpreter import GeminiInterpreter


class _ModelError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Models:
    def __init__(self, first_error: Exception | None = None) -> None:
        self.first_error = first_error
        self.calls = 0

    def generate_content(self, **_kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        if self.calls == 1 and self.first_error is not None:
            raise self.first_error
        return SimpleNamespace(text="{}")


def _interpreter(client, *, timeout: float = 1) -> GeminiInterpreter:  # noqa: ANN001
    return GeminiInterpreter(
        client_factory=lambda: client,
        model_id="fixture-model",
        prompt_version="commitment_interpretation_v2",
        schema_version="extraction_v2",
        thinking_level="low",
        controlled_display_name="Fixture User",
        request_timeout_seconds=timeout,
    )


def test_generate_does_not_retry_quota_auth_or_transport_errors() -> None:
    models = _Models(_ModelError(500, "thinking service unavailable"))
    interpreter = _interpreter(SimpleNamespace(models=models))

    with pytest.raises(_ModelError):
        interpreter._generate("prompt", {})

    assert models.calls == 1


def test_generate_retries_only_an_unsupported_thinking_config() -> None:
    models = _Models(_ModelError(400, "thinking config is not supported"))
    interpreter = _interpreter(SimpleNamespace(models=models))

    _response, _latency_ms, thinking_applied = interpreter._generate("prompt", {})

    assert models.calls == 2
    assert thinking_applied is False


async def test_application_deadline_caps_a_blocking_sdk_call(monkeypatch) -> None:  # noqa: ANN001
    interpreter = _interpreter(SimpleNamespace(), timeout=0.01)

    def slow_generate(_prompt, _schema):  # noqa: ANN001, ANN202
        time.sleep(0.05)
        return SimpleNamespace(text="{}"), 50, True

    monkeypatch.setattr(interpreter, "_generate", slow_generate)

    with pytest.raises(TimeoutError, match="application deadline"):
        await interpreter._generate_with_deadline("prompt", {})


def test_client_factory_sets_transport_timeout_and_disables_sdk_retries(
    monkeypatch,
) -> None:  # noqa: ANN001
    captured = {}

    class _Secrets:
        def access_secret_version(self, *, name):  # noqa: ANN001, ANN201
            assert name == "projects/test/secrets/sandbox/versions/1"
            return SimpleNamespace(payload=SimpleNamespace(data=b"sandbox-key"))

    marker = object()

    def client_factory(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        return marker

    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", _Secrets)
    monkeypatch.setattr(genai, "Client", client_factory)

    built = SecretManagerGeminiClientFactory(
        "projects/test/secrets/sandbox/versions/1",
        request_timeout_seconds=20,
    )()

    assert built is marker
    assert captured["api_key"] == "sandbox-key"
    assert captured["http_options"].timeout == 20_000
    assert captured["http_options"].retry_options.attempts == 1
