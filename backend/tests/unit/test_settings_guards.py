from __future__ import annotations

import pytest

from commitmentos.bootstrap.settings import RuntimeEnvironment, Settings


def settings_for(environment: RuntimeEnvironment, base_url: str) -> Settings:
    project = "commitmentos-test"
    secret = f"projects/{project}/secrets/example/versions/latest"
    return Settings.model_construct(
        environment=environment,
        controlled_user_id="controlled-01",
        controlled_email="controlled@example.invalid",
        calendar_webhook_path="/webhooks/calendar",
        service_base_url=base_url,
        oauth_redirect_uri=f"{base_url.rstrip('/')}/auth/callback",
        gmail_pubsub_topic=f"projects/{project}/topics/gmail",
        google_cloud_project=project,
        oauth_client_secret_ref=secret,
        controlled_refresh_token_secret_ref=secret,
        gemini_api_key_secret_ref=secret,
        calendar_channel_secret_ref=secret,
        policy_version="autonomy_policy_v2",
    )


def test_development_allows_local_http_configuration() -> None:
    settings_for(RuntimeEnvironment.DEVELOPMENT, "http://localhost:8000").validate_live_mode_guards()


def test_production_rejects_placeholder_origin() -> None:
    with pytest.raises(ValueError, match="placeholder"):
        settings_for(
            RuntimeEnvironment.PRODUCTION,
            "https://service.example",
        ).validate_live_mode_guards()


def test_production_rejects_a_policy_pin_that_does_not_match_runtime_behavior() -> None:
    settings = settings_for(
        RuntimeEnvironment.PRODUCTION,
        "https://commitmentos.run.app",
    )
    settings.policy_version = "autonomy_policy_v1"
    with pytest.raises(ValueError, match="autonomy_policy_v2"):
        settings.validate_live_mode_guards()
