from __future__ import annotations

import httpx

from commitmentos.bootstrap.cloud_run_contract import SANDBOX_UI_MARKERS, public_surface_errors

CURRENT_BUNDLE = " ".join(SANDBOX_UI_MARKERS)


def test_public_surface_contract_accepts_the_new_route_and_bundle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sandbox/api/messages":
            return httpx.Response(409, json={"detail": "sandbox session expired"})
        if request.url.path == "/sandbox":
            return httpx.Response(
                200,
                text='<html><script src="/app/assets/index-new.js"></script></html>',
            )
        if request.url.path == "/app/assets/index-new.js":
            return httpx.Response(200, text=CURRENT_BUNDLE)
        raise AssertionError(request.url)

    errors = public_surface_errors(
        "https://commitmentos.run.app",
        transport=httpx.MockTransport(handler),
    )

    assert errors == ()


def test_public_surface_contract_rejects_the_old_route_and_bundle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sandbox/api/messages":
            return httpx.Response(405)
        if request.url.path == "/sandbox":
            return httpx.Response(
                200,
                text='<html><script src="/app/assets/index-old.js"></script></html>',
            )
        if request.url.path == "/app/assets/index-old.js":
            return httpx.Response(200, text="old sandbox bundle")
        raise AssertionError(request.url)

    errors = public_surface_errors(
        "https://commitmentos.run.app",
        transport=httpx.MockTransport(handler),
    )

    assert len(errors) == 2
    assert any("endpoint is not active" in error for error in errors)
    assert any("current sandbox UI" in error for error in errors)


def test_public_surface_contract_rejects_stale_privacy_copy() -> None:
    stale_bundle = " ".join(
        marker
        for marker in SANDBOX_UI_MARKERS
        if marker != "held in this process until reset/expiry"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sandbox/api/messages":
            return httpx.Response(409)
        if request.url.path == "/sandbox":
            return httpx.Response(
                200,
                text='<html><script src="/app/assets/index-partial.js"></script></html>',
            )
        if request.url.path == "/app/assets/index-partial.js":
            return httpx.Response(200, text=stale_bundle)
        raise AssertionError(request.url)

    errors = public_surface_errors(
        "https://commitmentos.run.app",
        transport=httpx.MockTransport(handler),
    )

    assert len(errors) == 1
    assert "held in this process until reset/expiry" in errors[0]


def test_release_probe_requires_the_sandbox_specific_model_edge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sandbox/api/messages" and not request.headers.get(
            "X-Sandbox-Session"
        ):
            return httpx.Response(409)
        if request.url.path == "/sandbox":
            return httpx.Response(
                200,
                text='<html><script src="/app/assets/index-new.js"></script></html>',
            )
        if request.url.path == "/app/assets/index-new.js":
            return httpx.Response(200, text=CURRENT_BUNDLE)
        if request.url.path == "/sandbox/api/session":
            return httpx.Response(201, json={"sessionId": "fixture-session"})
        if request.url.path == "/sandbox/api/mode":
            return httpx.Response(200, json={"mode": "free_play"})
        if request.url.path == "/sandbox/api/messages":
            return httpx.Response(
                200,
                json={"interpretationSource": "custom-unavailable"},
            )
        raise AssertionError(request.url)

    errors = public_surface_errors(
        "https://commitmentos.run.app",
        transport=httpx.MockTransport(handler),
        probe_live_model=True,
    )

    assert len(errors) == 1
    assert "Gemini capability is not live" in errors[0]


def test_release_probe_rejects_a_cached_custom_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sandbox/api/messages" and not request.headers.get(
            "X-Sandbox-Session"
        ):
            return httpx.Response(409)
        if request.url.path == "/sandbox":
            return httpx.Response(
                200,
                text='<html><script src="/app/assets/index-new.js"></script></html>',
            )
        if request.url.path == "/app/assets/index-new.js":
            return httpx.Response(200, text=CURRENT_BUNDLE)
        if request.url.path == "/sandbox/api/session":
            return httpx.Response(201, json={"sessionId": "fixture-session"})
        if request.url.path == "/sandbox/api/mode":
            return httpx.Response(200, json={"mode": "free_play"})
        if request.url.path == "/sandbox/api/messages":
            return httpx.Response(200, json={"interpretationSource": "live-custom-cached"})
        raise AssertionError(request.url)

    errors = public_surface_errors(
        "https://commitmentos.run.app",
        transport=httpx.MockTransport(handler),
        probe_live_model=True,
    )

    assert len(errors) == 1
    assert "Gemini capability is not live" in errors[0]
