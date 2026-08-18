"""Owner-run Cloud Run release contract for the process-local judge sandbox."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urljoin

import httpx

from commitmentos.bootstrap.settings import RuntimeEnvironment, Settings

SERVICE_NAME = "commitmentos"
SANDBOX_MAX_INSTANCES = 2
SANDBOX_SECRET_ENV = "COMMITMENTOS_SANDBOX_GEMINI_API_KEY_SECRET_REF"
SANDBOX_UI_MARKERS = (
    "Choose a sandbox lane",
    "Try your own message",
    "Test the plan",
    "held in this process until reset/expiry",
)


def build_deploy_command(settings: Settings) -> tuple[str, ...]:
    """Return the single approved deployment shape; callers execute it."""

    settings.validate_live_mode_guards()
    if settings.environment is not RuntimeEnvironment.PRODUCTION:
        raise ValueError("owner deployment requires production settings")
    sandbox_secret_ref = settings.sandbox_gemini_api_key_secret_ref
    if sandbox_secret_ref is None:  # narrowed by the live guard above
        raise ValueError("sandbox Gemini secret reference is missing")
    return (
        "gcloud",
        "run",
        "deploy",
        SERVICE_NAME,
        "--source=.",
        f"--project={settings.google_cloud_project}",
        f"--region={settings.google_cloud_region}",
        "--allow-unauthenticated",
        "--session-affinity",
        f"--max-instances={SANDBOX_MAX_INSTANCES}",
        "--update-env-vars",
        f"{SANDBOX_SECRET_ENV}={sandbox_secret_ref}",
    )


def build_to_latest_command(settings: Settings) -> tuple[str, ...]:
    return (
        "gcloud",
        "run",
        "services",
        "update-traffic",
        SERVICE_NAME,
        "--to-latest",
        f"--project={settings.google_cloud_project}",
        f"--region={settings.google_cloud_region}",
    )


def build_describe_command(settings: Settings) -> tuple[str, ...]:
    return (
        "gcloud",
        "run",
        "services",
        "describe",
        SERVICE_NAME,
        f"--project={settings.google_cloud_project}",
        f"--region={settings.google_cloud_region}",
        "--format=json",
    )


def cloud_run_contract_errors(
    document: Mapping[str, Any],
    expected_sandbox_secret_ref: str,
) -> tuple[str, ...]:
    """Validate the effective latest-template and traffic configuration."""

    errors: list[str] = []
    spec = _mapping(document.get("spec"))
    template = _mapping(spec.get("template"))
    template_spec = _mapping(template.get("spec"))
    annotations = _mapping(_mapping(template.get("metadata")).get("annotations"))

    affinity_values = (
        template.get("sessionAffinity"),
        template_spec.get("sessionAffinity"),
        annotations.get("run.googleapis.com/sessionAffinity"),
    )
    if not any(_is_true(value) for value in affinity_values):
        errors.append("latest template does not enable session affinity")

    max_scale = annotations.get("autoscaling.knative.dev/maxScale")
    if str(max_scale) != str(SANDBOX_MAX_INSTANCES):
        errors.append(
            f"latest template maxScale is {max_scale!r}, expected "
            f"{SANDBOX_MAX_INSTANCES}"
        )

    containers = _sequence(template_spec.get("containers")) or _sequence(
        template.get("containers")
    )
    env: Sequence[Any] = ()
    if containers:
        env = _sequence(_mapping(containers[0]).get("env"))
    sandbox_ref = next(
        (
            _mapping(item).get("value")
            for item in env
            if _mapping(item).get("name") == SANDBOX_SECRET_ENV
        ),
        None,
    )
    if sandbox_ref != expected_sandbox_secret_ref:
        errors.append("latest template does not carry the expected sandbox Gemini secret")

    status = _mapping(document.get("status"))
    latest_ready = status.get("latestReadyRevisionName")
    positive_traffic = [
        _mapping(item)
        for item in _sequence(status.get("traffic"))
        if int(_mapping(item).get("percent") or 0) > 0
    ]
    latest_has_all_traffic = (
        len(positive_traffic) == 1
        and int(positive_traffic[0].get("percent") or 0) == 100
        and (
            _is_true(positive_traffic[0].get("latestRevision"))
            or (
                bool(latest_ready)
                and positive_traffic[0].get("revisionName") == latest_ready
            )
        )
    )
    if not latest_has_all_traffic:
        errors.append("latest ready revision does not receive 100% of traffic")
    return tuple(errors)


def public_surface_errors(
    service_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    probe_live_model: bool = False,
) -> tuple[str, ...]:
    """Prove the running API and frontend are from the free-play revision."""

    errors: list[str] = []
    origin = service_url.rstrip("/")
    try:
        with httpx.Client(
            timeout=30,
            follow_redirects=True,
            transport=transport,
        ) as client:
            response = client.post(
                f"{origin}/sandbox/api/messages",
                json={"sender": "jordan", "message": "release contract probe"},
            )
            if response.status_code != 409:
                errors.append(
                    "custom-message endpoint is not active "
                    f"(no-session probe returned HTTP {response.status_code})"
                )

            page = client.get(f"{origin}/sandbox")
            page.raise_for_status()
            sources = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', page.text)
            bundle_source = next(
                (source for source in sources if source.endswith(".js")), None
            )
            if bundle_source is None:
                errors.append("sandbox page does not reference a JavaScript bundle")
            else:
                bundle = client.get(urljoin(f"{origin}/sandbox", bundle_source))
                bundle.raise_for_status()
                missing_markers = [
                    marker for marker in SANDBOX_UI_MARKERS if marker not in bundle.text
                ]
                if missing_markers:
                    errors.append(
                        "served dashboard bundle is not the complete current sandbox UI "
                        f"(missing {missing_markers!r})"
                    )

            if probe_live_model:
                opened = client.post(f"{origin}/sandbox/api/session")
                if opened.status_code != 201:
                    errors.append(
                        "could not open a sandbox session for the live-model probe "
                        f"(HTTP {opened.status_code})"
                    )
                else:
                    session_id = str(opened.json().get("sessionId") or "")
                    headers = {"X-Sandbox-Session": session_id}
                    selected = client.post(
                        f"{origin}/sandbox/api/mode",
                        headers=headers,
                        json={
                            "mode": "free_play",
                            "subject": "Release contract probe",
                        },
                    )
                    custom = client.post(
                        f"{origin}/sandbox/api/messages",
                        headers=headers,
                        json={
                            "sender": "you",
                            "message": (
                                "I will send the vendor comparison deck by Friday "
                                "at 4 p.m."
                            ),
                        },
                    )
                    source = (
                        str(custom.json().get("interpretationSource") or "")
                        if selected.status_code == 200 and custom.status_code == 200
                        else ""
                    )
                    if (
                        selected.status_code != 200
                        or custom.status_code != 200
                        or source != "live-custom"
                    ):
                        errors.append(
                            "sandbox-specific Gemini capability is not live "
                            f"(mode HTTP {selected.status_code}, "
                            f"message HTTP {custom.status_code}, source={source!r})"
                        )
    except httpx.HTTPError as error:
        errors.append(f"public sandbox verification failed: {error}")
    return tuple(errors)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _is_true(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.casefold() == "true")
