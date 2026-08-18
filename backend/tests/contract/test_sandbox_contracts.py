"""Trust contracts for the interactive sandbox surface.

The sandbox is the only unauthenticated mutating surface in the service, so
these tests pin the properties that make that safe: a caller cannot act
without a session it created, cannot reach another session's world, cannot
submit an invalid free-play identity or unbounded text, and cannot exceed the
caps. The read-only `/demo` surface must stay mutation-free alongside it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from commitmentos.api.middleware.request_context import RequestContextMiddleware
from commitmentos.api.routers.sandbox import SESSION_HEADER, SandboxRouter
from commitmentos.sandbox.session import SandboxMode, SandboxSessionStore


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


def _choose(
    client: TestClient,
    session: str,
    mode: str,
    subject: str | None = None,
) -> dict:
    response = client.post(
        "/sandbox/api/mode",
        headers={SESSION_HEADER: session},
        json={"mode": mode, **({"subject": subject} if subject else {})},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestSandboxContracts:
    def test_session_is_required_for_every_action(self, client: TestClient) -> None:
        assert client.get("/sandbox/api/state").status_code == 409
        assert client.post("/sandbox/api/session/reset").status_code == 409
        assert (
            client.post("/sandbox/api/mode", json={"mode": "guided"}).status_code
            == 409
        )
        assert client.post("/sandbox/api/cards/msg_request").status_code == 409
        assert (
            client.post(
                "/sandbox/api/messages",
                json={"sender": "jordan", "message": "Can you help?"},
            ).status_code
            == 409
        )
        assert (
            client.post("/sandbox/api/messages/whatever/retry").status_code
            == 409
        )
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
        _choose(client, session, "guided")
        headers = {SESSION_HEADER: session}
        assert client.post("/sandbox/api/cards/../../etc", headers=headers).status_code in (
            404,
            405,
        )
        assert (
            client.post(
                "/sandbox/api/cards/drop_everything", headers=headers
            ).status_code
            == 404
        )

    def test_custom_message_identity_and_size_are_validated(
        self, client: TestClient
    ) -> None:
        session = _open(client)
        _choose(client, session, "free_play", "First principles")
        headers = {SESSION_HEADER: session}
        for payload in (
            {"sender": "someone-else", "message": "Can you help?"},
            {"sender": "jordan", "message": "   "},
            {"sender": "you", "message": "x" * 1001},
            {"sender": "you", "message": "hello\x00world"},
        ):
            assert (
                client.post(
                    "/sandbox/api/messages", headers=headers, json=payload
                ).status_code
                == 422
            )

    def test_one_person_can_send_multiple_custom_messages(
        self, client: TestClient
    ) -> None:
        session = _open(client)
        _choose(client, session, "free_play", "Project follow-up")
        headers = {SESSION_HEADER: session}
        first = client.post(
            "/sandbox/api/messages",
            headers=headers,
            json={"sender": "jordan", "message": "First thought."},
        )
        second = client.post(
            "/sandbox/api/messages",
            headers=headers,
            json={"sender": "jordan", "message": "One more detail."},
        )

        assert first.status_code == 200
        assert second.status_code == 200
        thread = second.json()["thread"]
        assert [row["card_id"] for row in thread] == [
            "sandbox-custom-1",
            "sandbox-custom-2",
        ]
        assert [row["persona"] for row in thread] == ["jordan", "jordan"]
        assert all(row["custom"] for row in thread)
        assert second.json()["interpretationSource"] == "custom-unavailable"

    def test_only_a_rejected_custom_message_can_be_retried(
        self,
        client: TestClient,
    ) -> None:
        session = _open(client)
        _choose(client, session, "free_play", "Project follow-up")
        response = client.post(
            "/sandbox/api/messages/not-rejected/retry",
            headers={SESSION_HEADER: session},
        )

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "interpretation retry is no longer available"
        )

    def test_custom_message_session_and_rolling_budgets_are_bounded(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(
            SandboxRouter(
                SandboxSessionStore(
                    live_interpreter=None,
                    max_custom_messages_per_session=2,
                    max_custom_messages_per_window=3,
                )
            ).build()
        )
        client = TestClient(app)
        first = _open(client)
        _choose(client, first, "free_play", "Status update")
        first_headers = {SESSION_HEADER: first}
        payload = {"sender": "you", "message": "Still working on it."}
        assert (
            client.post(
                "/sandbox/api/messages", headers=first_headers, json=payload
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/sandbox/api/messages", headers=first_headers, json=payload
            ).status_code
            == 200
        )
        limited = client.post(
            "/sandbox/api/messages", headers=first_headers, json=payload
        )
        assert limited.status_code == 429
        assert "start over" in limited.json()["detail"]

        second = _open(client)
        _choose(client, second, "free_play", "Status update")
        second_headers = {SESSION_HEADER: second}
        assert (
            client.post(
                "/sandbox/api/messages", headers=second_headers, json=payload
            ).status_code
            == 200
        )
        rolling = client.post(
            "/sandbox/api/messages", headers=second_headers, json=payload
        )
        assert rolling.status_code == 429
        assert "try again shortly" in rolling.json()["detail"]

    def test_cards_cannot_be_played_out_of_order(self, client: TestClient) -> None:
        session = _open(client)
        _choose(client, session, "guided")
        headers = {SESSION_HEADER: session}
        assert client.post("/sandbox/api/cards/check_in", headers=headers).status_code == 409
        # A rejected card is not charged against the budget or the deck.
        state = client.get("/sandbox/api/state", headers=headers).json()
        available = {row["card_id"]: row for row in state["cards"]}
        assert available["msg_request"]["available"]

    def test_sessions_cannot_see_each_other(self, client: TestClient) -> None:
        first = _open(client)
        second = _open(client)
        _choose(client, first, "guided")
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

    def test_reset_releases_the_old_world_before_replacement(self) -> None:
        store = SandboxSessionStore(live_interpreter=None, max_sessions=2)
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(SandboxRouter(store).build())
        client = TestClient(app)
        first = _open(client)
        second = _open(client)

        reset = client.post(
            "/sandbox/api/session/reset", headers={SESSION_HEADER: first}, json={}
        )
        assert reset.status_code == 201
        replacement = reset.json()["sessionId"]
        assert replacement not in {first, second}
        assert store.active_count() == 2
        assert (
            client.get("/sandbox/api/state", headers={SESSION_HEADER: first}).status_code
            == 409
        )
        assert (
            client.get(
                "/sandbox/api/state", headers={SESSION_HEADER: replacement}
            ).status_code
            == 200
        )
        assert client.post("/sandbox/api/session").status_code == 503

    async def test_concurrent_single_use_card_only_mutates_once(self) -> None:
        store = SandboxSessionStore(live_interpreter=None)
        router = SandboxRouter(store)
        session = store.create()
        store.select_mode(session, SandboxMode.GUIDED)

        results = await asyncio.gather(
            router.play(session.session_id, "msg_request"),
            router.play(session.session_id, "msg_request"),
            return_exceptions=True,
        )
        successes = [result for result in results if not isinstance(result, BaseException)]
        failures = [result for result in results if isinstance(result, HTTPException)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].status_code == 409
        assert session.cards_played == ["msg_request"]
        assert len(session.world.store.get("commitments", {})) == 1

    def test_guided_and_free_play_cannot_mix_and_subject_is_visible(
        self, client: TestClient
    ) -> None:
        session = _open(client)
        state = _choose(client, session, "free_play", "Quarterly tax estimate")
        headers = {SESSION_HEADER: session}

        assert state["mode"] == "free_play"
        assert state["threadSubject"] == "Quarterly tax estimate"
        assert {row["card_id"] for row in state["cards"]} == {
            "event_conflict",
            "advance_clock",
            "check_in",
        }
        assert not any(row["available"] for row in state["cards"])
        assert (
            client.post("/sandbox/api/cards/msg_request", headers=headers).status_code
            == 409
        )
        sent = client.post(
            "/sandbox/api/messages",
            headers=headers,
            json={"sender": "you", "message": "I will send it tomorrow."},
        )
        assert sent.status_code == 200
        assert sent.json()["thread"][0]["subject"] == "Quarterly tax estimate"
        switch = client.post(
            "/sandbox/api/mode",
            headers=headers,
            json={"mode": "guided"},
        )
        assert switch.status_code == 409
        assert "start over" in switch.json()["detail"]

    def test_session_creation_has_a_rolling_rate_limit(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(
            SandboxRouter(
                SandboxSessionStore(
                    live_interpreter=None,
                    max_sessions=10,
                    max_session_creations_per_window=2,
                )
            ).build()
        )
        client = TestClient(app)
        assert client.post("/sandbox/api/session").status_code == 201
        assert client.post("/sandbox/api/session").status_code == 201
        limited = client.post("/sandbox/api/session")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"

    def test_state_reads_do_not_keep_an_idle_world_alive(self) -> None:
        store = SandboxSessionStore(
            live_interpreter=None,
            idle_expiry=timedelta(minutes=45),
        )
        current = [datetime(2026, 9, 14, tzinfo=timezone.utc)]
        store._now = lambda: current[0]
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(SandboxRouter(store).build())
        client = TestClient(app)
        session = _open(client)
        headers = {SESSION_HEADER: session}

        current[0] += timedelta(minutes=44)
        assert client.get("/sandbox/api/state", headers=headers).status_code == 200
        current[0] += timedelta(minutes=2)
        assert client.get("/sandbox/api/state", headers=headers).status_code == 409

    async def test_expired_private_world_is_purged_without_another_request(
        self,
    ) -> None:
        store = SandboxSessionStore(
            live_interpreter=None,
            idle_expiry=timedelta(milliseconds=20),
            absolute_expiry=timedelta(seconds=1),
        )
        session = store.create()
        session_id = session.session_id
        session.thread_subject = "Private text"

        await asyncio.sleep(0.08)

        assert session_id not in store._sessions
        assert session_id not in store._expiry_handles

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
        assert (
            client.post(
                "/sandbox/api/approvals/x",
                headers=headers,
                json={"decision": "approve", "confirmed_minutes": 16},
            ).status_code
            == 422
        )
        # These are all real decision fields. A nonexistent approval reaches
        # the command and returns 409 rather than being stripped or rejected
        # by request parsing.
        complete = client.post(
            "/sandbox/api/approvals/x",
            headers=headers,
            json={
                "decision": "approve",
                "ownership_type": "my_commitment",
                "choice": "restore_approved_slot",
                "reason": "Judge supplied context",
            },
        )
        assert complete.status_code == 409
        assert "no longer pending" in complete.json()["detail"]
        assert (
            client.post(
                "/sandbox/api/approvals/x",
                headers=headers,
                json={"decision": "approve", "surprise": "silently ignored before"},
            ).status_code
            == 422
        )
