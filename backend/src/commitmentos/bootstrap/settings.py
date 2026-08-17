from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REQUIRED_POLICY_VERSION = "autonomy_policy_v2"


class RuntimeEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class OAuthPublishingMode(StrEnum):
    TESTING = "testing"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMMITMENTOS_", extra="forbid")

    environment: RuntimeEnvironment
    application_version: str
    service_base_url: AnyHttpUrl
    controlled_user_id: str
    controlled_email: str
    controlled_timezone: str
    google_cloud_project: str
    google_cloud_region: str
    firestore_database_id: str = "(default)"
    source_sync_queue: str
    reconciliation_queue: str
    calendar_actions_queue: str
    gmail_pubsub_topic: str
    pubsub_oidc_audience: str
    tasks_oidc_audience: str
    scheduler_oidc_audience: str
    pubsub_service_account: str
    tasks_service_account: str
    scheduler_service_account: str
    oauth_client_secret_ref: str
    controlled_refresh_token_secret_ref: str
    gemini_api_key_secret_ref: str
    oauth_publishing_mode: OAuthPublishingMode
    oauth_redirect_uri: AnyHttpUrl
    gemini_model_id: str
    gemini_thinking_level: str
    prompt_version: str
    extraction_schema_version: str
    task_schema_version: str
    observation_schema_version: str
    workflow_version: str
    planner_version: str
    risk_version: str
    policy_version: str
    calendar_event_id_algorithm_version: str
    calendar_webhook_path: str
    calendar_channel_secret_ref: str
    calendar_id: str
    demo_mode_enabled: bool = False
    demo_data_directory: Path
    maximum_sync_transaction_writes: int = Field(gt=0, le=400)
    maximum_sync_transaction_bytes: int = Field(gt=0, le=8 * 1024 * 1024)
    maximum_sync_apply_items_per_chunk: int = Field(gt=0)
    calendar_sync_page_size: int = Field(default=250, gt=0, le=250)
    sync_publication_barrier_probe_delay_seconds: float = Field(
        default=0, ge=0, le=30
    )
    maintenance_scan_limit: int = Field(gt=0)

    @classmethod
    def load(cls) -> Settings:
        return cls(_env_file=os.environ.get("COMMITMENTOS_ENV_FILE", ".env"))

    def validate_live_mode_guards(self) -> None:
        """Reject unsafe or placeholder live configuration before wiring I/O.

        Pydantic proves types; these checks prove cross-field deployment
        invariants that cannot be expressed as individual field constraints.
        Development and test remain able to use local HTTP endpoints.
        """
        if not self.controlled_user_id.strip() or not self.controlled_email.strip():
            raise ValueError("controlled user identity must be configured")
        if not self.calendar_webhook_path.startswith("/"):
            raise ValueError("calendar_webhook_path must be origin-relative")
        if self.environment != RuntimeEnvironment.PRODUCTION:
            return

        base_url = str(self.service_base_url).rstrip("/")
        redirect_url = str(self.oauth_redirect_uri)
        if not base_url.startswith("https://") or not redirect_url.startswith("https://"):
            raise ValueError("production service and OAuth redirect URLs must use HTTPS")
        if not redirect_url.startswith(f"{base_url}/"):
            raise ValueError("production OAuth redirect must use the service origin")
        if any(marker in base_url.lower() for marker in ("localhost", ".invalid", "example")):
            raise ValueError("production service URL cannot be a placeholder")
        if self.policy_version != REQUIRED_POLICY_VERSION:
            raise ValueError(
                f"production policy_version must be {REQUIRED_POLICY_VERSION}"
            )
        if not self.gmail_pubsub_topic.startswith(
            f"projects/{self.google_cloud_project}/topics/"
        ):
            raise ValueError("production Gmail topic must be a fully qualified project topic")
        for name, reference in (
            ("oauth_client_secret_ref", self.oauth_client_secret_ref),
            ("controlled_refresh_token_secret_ref", self.controlled_refresh_token_secret_ref),
            ("gemini_api_key_secret_ref", self.gemini_api_key_secret_ref),
            ("calendar_channel_secret_ref", self.calendar_channel_secret_ref),
        ):
            if not reference.startswith(f"projects/{self.google_cloud_project}/secrets/"):
                raise ValueError(f"{name} must reference Secret Manager in the live project")
            if "/versions/" not in reference:
                raise ValueError(f"{name} must include a Secret Manager version")

    def required_google_scopes(self) -> tuple[str, ...]:
        # scope_set_v1 — frozen in the Phase 0 checklist; changes require scope_set_v2.
        return (
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/gmail.readonly",
        )

    def calendar_webhook_url(self) -> str:
        return f"{str(self.service_base_url).rstrip('/')}{self.calendar_webhook_path}"
