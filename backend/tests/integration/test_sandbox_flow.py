"""The judge sandbox runs the real stack and tells the intended story.

These assertions are the guard on a public surface: the cards must produce
genuine extraction, convergence rather than duplication, a real plan, a real
automatic repair, and honest verified minutes — and the sandbox must never
reach a live credential or a durable document.
"""

from __future__ import annotations

import pytest

from commitmentos.sandbox import engine
from commitmentos.sandbox.session import SandboxSessionStore
from commitmentos.sandbox.world import SANDBOX_USER


@pytest.fixture
def store() -> SandboxSessionStore:
    return SandboxSessionStore(live_interpreter=None)


async def _play(session, card_id: str, store: SandboxSessionStore):  # noqa: ANN001
    store.ensure_budget(session)
    outcome = await engine.play_card(session, card_id)
    store.record_card(session, card_id)
    return outcome


async def _approve(session, request_type: str, **extra) -> None:  # noqa: ANN001
    view = await engine.render(session)
    match = next(
        (row for row in view["approvals"] if row["requestType"] == request_type), None
    )
    assert match is not None, f"no pending {request_type}: {view['approvals']}"
    await engine.resolve_approval(
        session, match["approvalId"], "approve", extra.get("confirmed_minutes")
    )


class TestSandboxFlow:
    async def test_thread_produces_one_converged_commitment(self, store) -> None:  # noqa: ANN001
        session = store.create()

        await _play(session, "msg_request", store)
        view = await engine.render(session)
        assert len(view["commitments"]) == 1
        assert view["commitments"][0]["ownershipType"] == "request_to_me"

        await _play(session, "msg_accept", store)
        view = await engine.render(session)
        # Acceptance converges onto the open request: still one commitment,
        # now owned by the user.
        assert len(view["commitments"]) == 1, "acceptance duplicated the commitment"
        assert view["commitments"][0]["ownershipType"] == "my_commitment"
        assert view["commitments"][0]["evidence"], "no evidence excerpt recorded"

    async def test_full_story_plans_repairs_and_verifies(self, store) -> None:  # noqa: ANN001
        session = store.create()
        await _play(session, "msg_request", store)
        await _play(session, "msg_accept", store)

        # Effort is confirmed by the user, never invented by the model.
        await _approve(session, "effort_confirmation", confirmed_minutes=180)
        view = await engine.render(session)
        assert view["commitments"][0]["confirmedMinutes"] == 180

        await _approve(session, "initial_plan_approval")
        view = await engine.render(session)
        blocks = view["blocks"]
        assert blocks, "plan approval produced no work blocks"
        assert sum(row["durationMinutes"] for row in blocks) == 180
        assert all(row["calendarEventId"] for row in blocks), "blocks not on calendar"

        # A meeting lands on the next planned block; the agent repairs it.
        planned = [row for row in blocks if row["executionState"] == "planned"]
        target = planned[0]
        outcome = await _play(session, "event_conflict", store)
        view = await engine.render(session)
        moved = next(
            row for row in view["blocks"] if row["workBlockId"] == target["workBlockId"]
        )
        assert moved["start"] != target["start"], outcome.headline
        assert moved["calendarEventId"] == target["calendarEventId"], (
            "repair replaced the calendar event instead of moving it"
        )

        # Elapse and check in: verified minutes are what the user confirms.
        await _play(session, "advance_clock", store)
        view = await engine.render(session)
        assert any(row["executionState"] == "awaiting_check_in" for row in view["blocks"])
        await _play(session, "check_in", store)
        view = await engine.render(session)
        assert sum(row["verifiedMinutes"] for row in view["blocks"]) == 60

    async def test_deadline_change_revises_one_commitment(self, store) -> None:  # noqa: ANN001
        session = store.create()
        await _play(session, "msg_request", store)
        await _play(session, "msg_accept", store)
        await _approve(session, "effort_confirmation", confirmed_minutes=180)
        await _approve(session, "initial_plan_approval")

        before = (await engine.render(session))["commitments"][0]
        await _play(session, "msg_deadline_change", store)
        after = (await engine.render(session))["commitments"][0]

        assert after["commitmentId"] == before["commitmentId"], "deadline change forked"
        assert after["revision"] > before["revision"], "revision did not advance"
        assert after["deadline"] < before["deadline"], "deadline did not move earlier"

    async def test_sessions_are_isolated(self, store) -> None:  # noqa: ANN001
        first = store.create()
        second = store.create()
        await _play(first, "msg_request", store)

        assert (await engine.render(first))["commitments"], "first session has no state"
        assert not (await engine.render(second))["commitments"], (
            "a second judge sees the first judge's commitments"
        )

    async def test_cards_gate_on_state(self, store) -> None:  # noqa: ANN001
        session = store.create()
        cards = {row["card_id"]: row for row in engine.available_cards(session)}
        assert cards["msg_request"]["available"]
        assert not cards["msg_accept"]["available"], "thread order is not enforced"
        assert not cards["check_in"]["available"]

        with pytest.raises(engine.SandboxCardError):
            await engine.play_card(session, "check_in")

    async def test_world_holds_no_live_surface(self, store) -> None:  # noqa: ANN001
        """The composition has no credential, client, or controlled user."""
        world = store.create().world
        assert world.actor().user_id == SANDBOX_USER != "controlled-01"
        for attribute in ("_credentials_provider", "_client", "credentials"):
            assert not hasattr(world.calendar_writer, attribute)
            assert not hasattr(world.gmail, attribute)
        assert world.store is not None and "commitments" not in world.store
