"""Trust contracts for the interactive sandbox surface.

The sandbox is the only unauthenticated mutating surface in the service, so
these tests pin the properties that make that safe: a caller cannot act
without a session it created, cannot reach another session's world, cannot
submit anything but a card id from the fixed deck, and cannot exceed the
caps. The read-only `/demo` surface must stay mutation-free alongside it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commitmentos.api.middleware.request_context import RequestContextMiddleware
from commitmentos.api.routers.sandbox import SESSION_HEADER, SandboxRouter
from commitmentos.sandbox.session import SandboxSessionStore


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(
        SandboxRouter(SandboxSessionStore(live_interpreter=None)).build()
    )
    return TestClient(app)


def _open(client: TestClient) -> str:
    response = client.post("/sandbox/api/session")
    assert response.status_code == 201
    return response.json()["sessionId"]


class TestSandboxContracts:
    def test_session_is_required_for_every_action(self, client: TestClient) -> None:
        assert client.get("/sandbox/api/state").status_code == 409
        assert client.post("/sandbox/api/cards/msg_request").status_code == 409
        assert (
            client.post(
                "/sandbox/api/approvals/whatever", json={"decision": "approve"}
            ).status_code
            == 409
        )

    def test_forged_session_id_is_rejected(self, client: TestClient) -> None:
        _open(client)
        response = client.get(
            "/sandbox/api/state", headers={SESSION_HEADER: "not-a-real-session"}
        )
        assert response.status_code == 409

    def test_only_deck_cards_are_playable(self, client: TestClient) -> None:
        session = _open(client)
        headers = {SESSION_HEADER: session}
        assert client.post("/sandbox/api/cards/../../etc", headers=headers).status_code in (
            404,
            405,
        )
        assert (
            client.post("/sandbox/api/cards/drop_everything", headers=headers).status_code
            == 404
        )

    def test_cards_cannot_be_played_out_of_order(self, client: TestClient) -> None:
        session = _open(client)
        headers = {SESSION_HEADER: session}
        assert client.post("/sandbox/api/cards/check_in", headers=headers).status_code == 409
        # A rejected card is not charged against the budget or the deck.
        state = client.get("/sandbox/api/state", headers=headers).json()
        available = {row["card_id"]: row for row in state["cards"]}
        assert available["msg_request"]["available"]

    def test_sessions_cannot_see_each_other(self, client: TestClient) -> None:
        first = _open(client)
        second = _open(client)
        played = client.post(
            "/sandbox/api/cards/msg_request", headers={SESSION_HEADER: first}
        )
        assert played.status_code == 200
        assert played.json()["commitments"]

        other = client.get("/sandbox/api/state", headers={SESSION_HEADER: second}).json()
        assert other["commitments"] == []
        assert other["thread"] == []

    def test_capacity_is_bounded(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(
            SandboxRouter(
                SandboxSessionStore(live_interpreter=None, max_sessions=2)
            ).build()
        )
        client = TestClient(app)
        assert client.post("/sandbox/api/session").status_code == 201
        assert client.post("/sandbox/api/session").status_code == 201
        assert client.post("/sandbox/api/session").status_code == 503

    def test_api_routes_are_not_shadowed_by_the_spa_catch_all(self) -> None:
        """Data routes must outrank the SPA catch-all, as they do in main.py.

        The demo surface shipped this bug once: its data routes sat directly
        under the page prefix, so a hard refresh of a page URL returned raw
        JSON. Registration order is what keeps `/sandbox/api/*` serving the
        API while `/sandbox/anything` serves the app shell.
        """
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(
            SandboxRouter(SandboxSessionStore(live_interpreter=None)).build()
        )

        @app.get("/sandbox")
        @app.get("/sandbox/{rest:path}")
        async def spa(rest: str = "") -> dict[str, str]:
            del rest
            return {"served": "spa"}

        client = TestClient(app)
        assert client.get("/sandbox").json() == {"served": "spa"}
        assert client.get("/sandbox/whatever").json() == {"served": "spa"}
        # The API route wins despite matching the catch-all pattern.
        assert client.get("/sandbox/api/state").status_code == 409

    def test_approval_decision_body_is_validated(self, client: TestClient) -> None:
        session = _open(client)
        headers = {SESSION_HEADER: session}
        assert (
            client.post(
                "/sandbox/api/approvals/x", headers=headers, json={"decision": "delete"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/sandbox/api/approvals/x",
                headers=headers,
                json={"decision": "approve", "confirmed_minutes": 999999},
            ).status_code
            == 422
        )
