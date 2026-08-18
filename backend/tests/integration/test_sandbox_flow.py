"""The judge sandbox runs the real stack and tells the intended story.

These assertions are the guard on a public surface: the cards must produce
genuine extraction, convergence rather than duplication, a real plan, a real
automatic repair, and honest verified minutes — and the sandbox must never
reach a controlled-user credential or a durable document.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from commitmentos.contracts.model_output import (
    CommitmentInterpretationV1,
    derive_span_key,
    parse_interpretation_wire,
    wire_to_contract,
)
from commitmentos.domain.commitments.identity import IdentityOperation
from commitmentos.domain.commitments.models import OwnershipType
from commitmentos.sandbox import engine
from commitmentos.sandbox.scenario import MESSAGES, THREAD_SUBJECT
from commitmentos.sandbox.session import (
    SandboxMode,
    SandboxModeError,
    SandboxSessionStore,
)
from commitmentos.sandbox.twin import FakeModelInterpreter
from commitmentos.sandbox.world import SANDBOX_USER


@pytest.fixture
def store() -> SandboxSessionStore:
    return SandboxSessionStore(live_interpreter=None)


async def _play(session, card_id: str, store: SandboxSessionStore):  # noqa: ANN001
    if session.mode is SandboxMode.UNSELECTED:
        store.select_mode(session, SandboxMode.GUIDED)
    store.ensure_budget(session)
    outcome = await engine.play_card(session, card_id)
    store.record_card(session, card_id, outcome.detail)
    return outcome


def _recorded(card_id: str):  # noqa: ANN202
    card = next(card for card in MESSAGES if card.card_id == card_id)
    wire, errors = parse_interpretation_wire(card.recorded_wire)
    assert wire is not None, errors
    return wire_to_contract(wire)


async def _approve(session, request_type: str, **extra) -> None:  # noqa: ANN001
    view = await engine.render(session)
    match = next(
        (row for row in view["approvals"] if row["requestType"] == request_type), None
    )
    assert match is not None, f"no pending {request_type}: {view['approvals']}"
    await engine.resolve_approval(
        session,
        match["approvalId"],
        {"decision": "approve", **extra},
    )


class TestSandboxFlow:
    async def test_neutral_ignore_proposals_explain_why_state_did_not_change(
        self,
    ) -> None:
        body = "Thanks for the update. No action is needed."
        interpretation = _recorded("msg_accept")
        base = interpretation.proposals[0]
        proposals = tuple(
            replace(
                base,
                normalized_outcome=outcome,
                proposed_identity_operation=IdentityOperation.IGNORE,
                target_commitment_id=None,
                deadline=None,
                proposed_effort_minutes=None,
                source_span_key=derive_span_key("sandbox-custom-1", quote),
                evidence=(
                    replace(
                        base.evidence[0],
                        message_id="sandbox-custom-1",
                        excerpt=quote,
                        span_key=derive_span_key("sandbox-custom-1", quote),
                    ),
                ),
            )
            for quote, outcome in (
                ("Thanks for the update.", "Acknowledge the update"),
                ("No action is needed.", "Take no action"),
            )
        )
        live = FakeModelInterpreter()
        live.script(replace(interpretation, proposals=proposals))
        store = SandboxSessionStore(live_interpreter=live)
        session = store.create()
        store.select_mode(session, SandboxMode.FREE_PLAY, subject="Status")

        message_id = store.charge_custom_message(session)
        outcome = await engine.play_custom_message(
            session,
            message_id,
            "jordan",
            body,
        )
        store.record_custom_message(
            session,
            message_id,
            "jordan",
            body,
            outcome.detail,
        )
        view = await engine.render(session)

        assert view["commitments"] == []
        assert any(
            row["summary"]
            == (
                "Interpreted 2 proposals: 2 ignore operations; "
                "durable commitment state unchanged"
            )
            for row in view["activity"]
        )

    async def test_custom_message_runs_live_and_never_borrows_a_card_fallback(
        self,
    ) -> None:
        body = MESSAGES[0].body

        unavailable_store = SandboxSessionStore(live_interpreter=None)
        unavailable = unavailable_store.create()
        unavailable_store.select_mode(
            unavailable, SandboxMode.FREE_PLAY, subject="Copied guided message"
        )
        message_id = unavailable_store.charge_custom_message(unavailable)
        outcome = await engine.play_custom_message(
            unavailable, message_id, "jordan", body
        )
        unavailable_store.record_custom_message(
            unavailable, message_id, "jordan", body, outcome.detail
        )
        unavailable_view = await engine.render(unavailable)
        assert unavailable_view["commitments"] == []
        assert unavailable_view["interpretationSource"] == "custom-unavailable"
        assert "No canned interpretation was substituted" in outcome.detail
        # Even text identical to a guided card cannot select its recorded
        # answer, and the separate guided lane cannot be mixed into this world.
        with pytest.raises(SandboxModeError, match="start over"):
            unavailable_store.select_mode(unavailable, SandboxMode.GUIDED)
        free_play_cards = engine.available_cards(unavailable)
        assert all(row["kind"] != "message" for row in free_play_cards)
        assert not any(row["available"] for row in free_play_cards)

        live = FakeModelInterpreter()
        interpretation = _recorded("msg_request")
        proposal = interpretation.proposals[0]
        quote = "Could you put together the vendor comparison deck?"
        evidence = proposal.evidence[0]
        live.script(
            replace(
                interpretation,
                proposals=(
                    replace(
                        proposal,
                        source_span_key=derive_span_key("sandbox-custom-1", quote),
                        evidence=(
                            replace(
                                evidence,
                                message_id="sandbox-custom-1",
                                excerpt=quote,
                                span_key=derive_span_key("sandbox-custom-1", quote),
                            ),
                        ),
                    ),
                ),
            )
        )
        live_store = SandboxSessionStore(live_interpreter=live)
        session = live_store.create()
        live_store.select_mode(
            session, SandboxMode.FREE_PLAY, subject="Copied guided message"
        )
        message_id = live_store.charge_custom_message(session)
        outcome = await engine.play_custom_message(session, message_id, "jordan", body)
        live_store.record_custom_message(
            session, message_id, "jordan", body, outcome.detail
        )
        view = await engine.render(session)

        assert len(view["commitments"]) == 1
        assert view["commitments"][0]["ownershipType"] == "request_to_me"
        assert view["interpretationSource"] == "live-custom"
        assert live.calls[0]["source_text"].count(body) == 1
        assert 'subject="Copied guided message"' in live.calls[0]["source_text"]
        assert THREAD_SUBJECT not in live.calls[0]["source_text"]

    async def test_same_selected_person_can_send_repeated_live_messages(self) -> None:
        live = FakeModelInterpreter()
        live.script(CommitmentInterpretationV1(schema_version="extraction_v2", proposals=()))
        live.script(CommitmentInterpretationV1(schema_version="extraction_v2", proposals=()))
        store = SandboxSessionStore(live_interpreter=live)
        session = store.create()
        store.select_mode(session, SandboxMode.FREE_PLAY, subject="Status updates")

        for body in ("I have another detail.", "And one final note."):
            message_id = store.charge_custom_message(session)
            outcome = await engine.play_custom_message(session, message_id, "you", body)
            store.record_custom_message(
                session, message_id, "you", body, outcome.detail
            )

        view = await engine.render(session)
        assert [row["persona"] for row in view["thread"]] == ["you", "you"]
        assert [row["body"] for row in view["thread"]] == [
            "I have another detail.",
            "And one final note.",
        ]
        assert view["customMessagesRemaining"] == 6
        assert all(
            "SENT" in session.world.gmail.messages[row["card_id"]].label_ids
            for row in view["thread"]
        )
        assert len(live.calls) == 2

    async def test_rejected_custom_interpretation_can_retry_same_message_once(
        self,
    ) -> None:
        body = "I'll send Jordan the budget memo by Friday."
        interpretation = _recorded("msg_accept")
        base = interpretation.proposals[0]
        valid = replace(
            base,
            normalized_outcome="Send Jordan the budget memo",
            proposed_identity_operation=IdentityOperation.CREATE,
            target_commitment_id=None,
            source_span_key=derive_span_key("sandbox-custom-1", body),
            evidence=(
                replace(
                    base.evidence[0],
                    message_id="sandbox-custom-1",
                    excerpt=body,
                    span_key=derive_span_key("sandbox-custom-1", body),
                ),
            ),
        )
        invalid_quote = "This quote is not in the message."
        invalid = replace(
            valid,
            source_span_key=derive_span_key("sandbox-custom-1", invalid_quote),
            evidence=(
                replace(
                    valid.evidence[0],
                    excerpt=invalid_quote,
                    span_key=derive_span_key(
                        "sandbox-custom-1",
                        invalid_quote,
                    ),
                ),
            ),
        )
        live = FakeModelInterpreter()
        live.script(replace(interpretation, proposals=(invalid,)))
        live.script(replace(interpretation, proposals=(valid,)))
        store = SandboxSessionStore(live_interpreter=live)
        session = store.create()
        store.select_mode(session, SandboxMode.FREE_PLAY, subject="Budget memo")

        message_id = store.charge_custom_message(session)
        first = await engine.play_custom_message(session, message_id, "you", body)
        store.record_custom_message(
            session,
            message_id,
            "you",
            body,
            first.detail,
        )
        rejected_view = await engine.render(session)
        assert rejected_view["commitments"] == []
        assert rejected_view["thread"][0]["retryAvailable"] is True
        assert rejected_view["thread"][0]["retryAttemptsRemaining"] == 1

        attempt = store.charge_interpretation_retry(session, message_id)
        retried = await engine.retry_custom_message(session, message_id, attempt)
        store.record_interpretation_retry(session, message_id, retried.detail)
        recovered_view = await engine.render(session)

        assert len(recovered_view["thread"]) == 1
        assert len(session.world.gmail.messages) == 1
        assert len(recovered_view["commitments"]) == 1
        assert recovered_view["commitments"][0]["title"] == (
            "Send Jordan the budget memo"
        )
        assert recovered_view["thread"][0]["retryAvailable"] is False
        assert recovered_view["thread"][0]["retryAttemptsRemaining"] == 0
        assert session.custom_messages_sent == 1
        assert session.interpretation_retries_sent == 1
        assert len(live.calls) == 2

    async def test_free_play_retraction_narrates_the_cancellation(self) -> None:
        promise_body = "I'll send you the quarterly report by Friday."
        retraction_body = (
            "Actually, I can't send it anymore, so please disregard my earlier promise."
        )
        interpretation = _recorded("msg_accept")
        base = interpretation.proposals[0]
        promise = replace(
            base,
            normalized_outcome="Send the quarterly report",
            ownership_type=OwnershipType.COMMITMENT_TO_ME,
            proposed_identity_operation=IdentityOperation.CREATE,
            target_commitment_id=None,
            source_span_key=derive_span_key("sandbox-custom-1", promise_body),
            evidence=(
                replace(
                    base.evidence[0],
                    message_id="sandbox-custom-1",
                    excerpt=promise_body,
                    span_key=derive_span_key("sandbox-custom-1", promise_body),
                ),
            ),
        )
        unsafe_retraction = replace(
            promise,
            source_span_key=derive_span_key("sandbox-custom-2", retraction_body),
            evidence=(
                replace(
                    base.evidence[0],
                    message_id="sandbox-custom-2",
                    excerpt=retraction_body,
                    span_key=derive_span_key(
                        "sandbox-custom-2", retraction_body
                    ),
                ),
            ),
        )
        live = FakeModelInterpreter()
        live.script(replace(interpretation, proposals=(promise,)))
        live.script(replace(interpretation, proposals=(unsafe_retraction,)))
        store = SandboxSessionStore(live_interpreter=live)
        session = store.create()
        store.select_mode(session, SandboxMode.FREE_PLAY, subject="Quarterly report")

        first_id = store.charge_custom_message(session)
        first = await engine.play_custom_message(
            session, first_id, "jordan", promise_body
        )
        store.record_custom_message(
            session, first_id, "jordan", promise_body, first.detail
        )
        second_id = store.charge_custom_message(session)
        second = await engine.play_custom_message(
            session, second_id, "jordan", retraction_body
        )
        store.record_custom_message(
            session, second_id, "jordan", retraction_body, second.detail
        )

        view = await engine.render(session)
        assert second.detail == (
            "The explicit retraction canceled the existing commitment without "
            "creating another one."
        )
        assert len(view["commitments"]) == 1
        assert view["commitments"][0]["lifecycleStatus"] == "canceled"
        assert view["commitments"][0]["revision"] == 2
        assert view["approvals"] == []
        assert not any(
            row["available"] is False
            and row["blocked_reason"] == "Resolve the pending confirmation first"
            for row in view["cards"]
        )

    async def test_custom_results_never_enter_the_process_shared_cache(self) -> None:
        live = FakeModelInterpreter()
        empty = CommitmentInterpretationV1(
            schema_version="extraction_v2", proposals=()
        )
        live.script(empty)
        live.script(empty)
        store = SandboxSessionStore(live_interpreter=live)

        for _ in range(2):
            session = store.create()
            store.select_mode(
                session, SandboxMode.FREE_PLAY, subject="Private experiment"
            )
            message_id = store.charge_custom_message(session)
            await engine.play_custom_message(
                session, message_id, "you", "No commitment in this note."
            )

        assert len(live.calls) == 2
        assert store._interpretation_cache.size() == 0

    async def test_custom_effort_survives_the_approval_view_unchanged(self) -> None:
        body = "I will prepare the hiring plan in 45 minutes by Friday."
        interpretation = _recorded("msg_accept")
        proposal = interpretation.proposals[0]
        evidence = proposal.evidence[0]
        customized = replace(
            proposal,
            normalized_outcome="Prepare the hiring plan",
            description="Prepare the hiring plan for the team.",
            proposed_effort_minutes=45,
            proposed_identity_operation=IdentityOperation.CREATE,
            target_commitment_id=None,
            source_span_key=derive_span_key("sandbox-custom-1", body),
            deadline=(
                replace(proposal.deadline, source_expression="by Friday")
                if proposal.deadline
                else None
            ),
            evidence=(
                replace(
                    evidence,
                    message_id="sandbox-custom-1",
                    excerpt=body,
                    span_key=derive_span_key("sandbox-custom-1", body),
                ),
            ),
        )
        live = FakeModelInterpreter()
        live.script(replace(interpretation, proposals=(customized,)))
        store = SandboxSessionStore(live_interpreter=live)
        session = store.create()
        store.select_mode(session, SandboxMode.FREE_PLAY, subject="Hiring plan")

        message_id = store.charge_custom_message(session)
        await engine.play_custom_message(session, message_id, "you", body)
        view = await engine.render(session)

        effort = next(
            approval
            for approval in view["approvals"]
            if approval["requestType"] == "effort_confirmation"
        )
        assert effort["proposedMinutes"] == 45
        assert effort["commitmentTitle"] == "Prepare the hiring plan"

        await _approve(session, "effort_confirmation", confirmed_minutes=45)
        await _approve(session, "initial_plan_approval")
        planned = await engine.render(session)
        assert planned["blocks"]
        assert all(
            block["title"] == "Prepare the hiring plan"
            for block in planned["blocks"]
        )

        # Free play can continue beyond email extraction through the same
        # conflict, repair, elapsed-block, and honest check-in loop.
        cards = {row["card_id"]: row for row in planned["cards"]}
        assert cards["event_conflict"]["available"]
        for card_id in ("event_conflict", "advance_clock", "check_in"):
            outcome = await engine.play_card(session, card_id)
            store.record_card(session, card_id, outcome.detail)
        exercised = await engine.render(session)
        assert sum(row["verifiedMinutes"] for row in exercised["blocks"]) == 45
        assert "45 verified minutes" in outcome.headline

        commitment_id = exercised["commitments"][0]["commitmentId"]
        await engine.complete_commitment(session, commitment_id)
        stored = session.world.store["commitments"][commitment_id]
        evidence = session.world.store["evidence"][stored["completion_evidence_id"]]
        assert evidence["note"] == "Completed: Prepare the hiring plan."
        assert "vendor" not in evidence["note"].casefold()

    async def test_ambiguous_free_play_approval_accepts_confirmed_ownership(
        self,
    ) -> None:
        body = "Someone should handle the venue booking before Friday."
        interpretation = _recorded("msg_accept")
        proposal = interpretation.proposals[0]
        evidence = proposal.evidence[0]
        quote = "Someone should handle the venue booking"
        ambiguous = replace(
            proposal,
            normalized_outcome="Handle the venue booking",
            description="The message does not say who owns the venue booking.",
            ownership_type=OwnershipType.AMBIGUOUS,
            proposed_identity_operation=IdentityOperation.AMBIGUOUS,
            target_commitment_id=None,
            source_span_key=derive_span_key("sandbox-custom-1", quote),
            evidence=(
                replace(
                    evidence,
                    message_id="sandbox-custom-1",
                    excerpt=quote,
                    span_key=derive_span_key("sandbox-custom-1", quote),
                ),
            ),
        )
        live = FakeModelInterpreter()
        live.script(replace(interpretation, proposals=(ambiguous,)))
        store = SandboxSessionStore(live_interpreter=live)
        session = store.create()
        store.select_mode(session, SandboxMode.FREE_PLAY, subject="Venue booking")

        message_id = store.charge_custom_message(session)
        outcome = await engine.play_custom_message(session, message_id, "jordan", body)
        store.record_custom_message(
            session, message_id, "jordan", body, outcome.detail
        )
        view = await engine.render(session)
        approval = next(
            row
            for row in view["approvals"]
            if row["requestType"] == "identity_confirmation"
        )
        assert approval["requiresOwnership"] is True
        assert approval["ownershipOptions"] == [
            "my_commitment",
            "request_to_me",
            "commitment_to_me",
        ]
        assert approval["normalizedOutcome"] == "Handle the venue booking"

        await engine.resolve_approval(
            session,
            approval["approvalId"],
            {"decision": "approve", "ownership_type": "my_commitment"},
        )
        resolved = await engine.render(session)
        assert len(resolved["commitments"]) == 1
        assert resolved["commitments"][0]["ownershipType"] == "my_commitment"

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
        repair_summaries = [
            row["summary"]
            for row in view["activity"]
            if row["eventType"] == "plan_repaired"
        ]
        assert any("moved 1 block" in summary for summary in repair_summaries)
        assert all("block(s)" not in summary for summary in repair_summaries)

        # Elapse and check in: verified minutes are what the user confirms.
        await _play(session, "advance_clock", store)
        view = await engine.render(session)
        assert any(row["executionState"] == "awaiting_check_in" for row in view["blocks"])
        assert any(
            "awaiting verified-minute check-in" in row["summary"]
            for row in view["activity"]
        )
        assert not any(
            row["summary"] == "Portfolio replanned after verified progress changed"
            for row in view["activity"]
        )
        await _play(session, "check_in", store)
        view = await engine.render(session)
        assert sum(row["verifiedMinutes"] for row in view["blocks"]) == 60

        commitment_id = view["commitments"][0]["commitmentId"]
        await engine.complete_commitment(session, commitment_id)
        completed = await engine.render(session)
        commitment = completed["commitments"][0]
        assert commitment["lifecycleStatus"] == "completed"
        assert commitment["verifiedMinutes"] == 60
        assert commitment["remainingMinutes"] is None
        assert commitment["riskLevel"] is None
        excerpts = [row["excerpt"].strip() for row in commitment["evidence"]]
        assert all(excerpts)
        assert len(excerpts) == len(set(excerpts))
        assert sum(
            block["durationMinutes"]
            for block in completed["blocks"]
            if block["executionState"] in {"planned", "active"}
        ) == 0
        assert sum(
            block["durationMinutes"]
            for block in completed["blocks"]
            if block["executionState"] == "canceled"
        ) == 120
        assert any(
            row["summary"]
            == (
                "Portfolio replanned after commitment completion; "
                "verified progress unchanged"
            )
            for row in completed["activity"]
        )

    async def test_rejected_first_plan_keeps_known_remaining_effort(
        self, store
    ) -> None:  # noqa: ANN001
        session = store.create()
        await _play(session, "msg_request", store)
        await _play(session, "msg_accept", store)
        await _approve(session, "effort_confirmation", confirmed_minutes=30)
        proposed = await engine.render(session)
        approval = next(
            row
            for row in proposed["approvals"]
            if row["requestType"] == "initial_plan_approval"
        )

        await engine.resolve_approval(
            session,
            approval["approvalId"],
            {"decision": "reject", "reason": "The time does not work"},
        )
        rejected = await engine.render(session)
        commitment = rejected["commitments"][0]

        assert commitment["confirmedMinutes"] == 30
        assert commitment["verifiedMinutes"] == 0
        assert commitment["remainingMinutes"] == 30
        reconsideration = rejected["approvals"][0]
        assert reconsideration["previousRejectionReason"] == (
            "The time does not work"
        )

    async def test_deadline_proposal_waits_then_revises_one_commitment(
        self, store
    ) -> None:  # noqa: ANN001
        session = store.create()
        await _play(session, "msg_request", store)
        await _play(session, "msg_accept", store)
        await _approve(session, "effort_confirmation", confirmed_minutes=180)
        await _approve(session, "initial_plan_approval")

        before = (await engine.render(session))["commitments"][0]
        await _play(session, "msg_deadline_change", store)
        proposed = await engine.render(session)
        unchanged = proposed["commitments"][0]

        assert unchanged["commitmentId"] == before["commitmentId"]
        assert unchanged["revision"] == before["revision"]
        assert unchanged["deadline"] == before["deadline"]
        approval = next(
            row
            for row in proposed["approvals"]
            if row["requestType"] == "deadline_change_confirmation"
        )
        assert approval["reason"] == (
            "counterparty_deadline_change_requires_confirmation"
        )
        assert approval["proposedDeadline"] < before["deadline"]

        assert "no commitment revision was claimed" in proposed["thread"][-1]["note"]
        await _approve(session, "deadline_change_confirmation")
        after = (await engine.render(session))["commitments"][0]

        assert after["commitmentId"] == before["commitmentId"], "deadline change forked"
        assert after["revision"] > before["revision"], "revision did not advance"
        assert after["deadline"] < before["deadline"], "deadline did not move earlier"
        assert after["evidence"][0]["supportsDeadline"] is True
        assert "Thursday" in after["evidence"][0]["excerpt"]
        accepted = await engine.render(session)
        assert accepted["thread"][-1]["note"] == (
            "The proposal was held for confirmation and later accepted by you."
        )
        assert any(
            event["eventType"] == "deadline_change_confirmation"
            and event["summary"] == "deadline_change_confirmation approved"
            for event in accepted["activity"]
        )

    async def test_story_order_keeps_deadline_and_conflict_prospective(self, store) -> None:  # noqa: ANN001
        session = store.create()
        await _play(session, "msg_request", store)
        await _play(session, "msg_accept", store)

        cards = {row["card_id"]: row for row in engine.available_cards(session)}
        assert not cards["msg_deadline_change"]["available"]
        assert not cards["event_conflict"]["available"]
        assert not cards["advance_clock"]["available"]

        await _approve(session, "effort_confirmation", confirmed_minutes=180)
        cards = {row["card_id"]: row for row in engine.available_cards(session)}
        assert not cards["msg_deadline_change"]["available"]

        await _approve(session, "initial_plan_approval")
        cards = {row["card_id"]: row for row in engine.available_cards(session)}
        assert cards["msg_deadline_change"]["available"]
        assert not cards["event_conflict"]["available"]

        await _play(session, "msg_deadline_change", store)
        view = await engine.render(session)
        next_block = next(
            block for block in view["blocks"] if block["executionState"] == "planned"
        )
        assert datetime.fromisoformat(next_block["start"]) > session.world.clock.now()
        cards = {row["card_id"]: row for row in view["cards"]}
        assert not cards["event_conflict"]["available"]
        await _approve(session, "deadline_change_confirmation")
        view = await engine.render(session)
        cards = {row["card_id"]: row for row in view["cards"]}
        assert cards["event_conflict"]["available"]
        assert not cards["advance_clock"]["available"]

        await _play(session, "event_conflict", store)
        cards = {row["card_id"]: row for row in engine.available_cards(session)}
        assert cards["advance_clock"]["available"]

    async def test_live_semantic_drift_falls_back_and_cache_keeps_provenance_local(
        self,
    ) -> None:
        live = FakeModelInterpreter()
        live.script(_recorded("msg_request"))
        live.script(_recorded("msg_accept"))
        deadline = _recorded("msg_deadline_change")
        wrong_proposal = replace(
            deadline.proposals[0],
            ownership_type=OwnershipType.REQUEST_TO_ME,
            proposed_identity_operation=IdentityOperation.CREATE,
            target_commitment_id=None,
            normalized_outcome="Get the vendor comparison deck by Thursday",
        )
        live.script(replace(deadline, proposals=(wrong_proposal,)))
        store = SandboxSessionStore(live_interpreter=live)

        first = store.create()
        assert (await engine.render(first))["interpretationSource"] == "not-run"
        await _play(first, "msg_request", store)
        await _play(first, "msg_accept", store)
        await _approve(first, "effort_confirmation", confirmed_minutes=180)
        await _approve(first, "initial_plan_approval")
        before = (await engine.render(first))["commitments"][0]
        outcome = await _play(first, "msg_deadline_change", store)
        first_view = await engine.render(first)

        assert outcome.headline == (
            "The agent paused for confirmation before changing anything"
        )
        assert first_view["interpretationSource"] == "recorded-fallback"
        assert len(first_view["commitments"]) == 1
        assert first_view["commitments"][0]["revision"] == before["revision"]
        assert first_view["commitments"][0]["deadline"] == before["deadline"]
        assert any(
            row["requestType"] == "deadline_change_confirmation"
            for row in first_view["approvals"]
        )
        await _approve(first, "deadline_change_confirmation")
        accepted = await engine.render(first)
        assert accepted["commitments"][0]["revision"] > before["revision"]
        assert accepted["commitments"][0]["deadline"] < before["deadline"]

        second = store.create()
        assert (await engine.render(second))["interpretationSource"] == "not-run"
        await _play(second, "msg_request", store)
        await _play(second, "msg_accept", store)
        await _approve(second, "effort_confirmation", confirmed_minutes=180)
        await _approve(second, "initial_plan_approval")
        await _play(second, "msg_deadline_change", store)
        await _approve(second, "deadline_change_confirmation")
        second_view = await engine.render(second)

        assert second_view["interpretationSource"] == "recorded-fallback-cached"
        assert len(live.calls) == 3, "the shared fixed-input cache was bypassed"

    async def test_message_outcome_exposes_a_duplicate_instead_of_claiming_revision(
        self, store
    ) -> None:  # noqa: ANN001
        session = store.create()
        await _play(session, "msg_request", store)
        await _play(session, "msg_accept", store)
        await _approve(session, "effort_confirmation", confirmed_minutes=180)
        await _approve(session, "initial_plan_approval")
        existing_id = (await engine.render(session))["commitments"][0]["commitmentId"]

        wrong = _recorded("msg_deadline_change")
        wrong_proposal = replace(
            wrong.proposals[0],
            ownership_type=OwnershipType.REQUEST_TO_ME,
            proposed_identity_operation=IdentityOperation.UPDATE_EXISTING,
            target_commitment_id=existing_id,
            normalized_outcome="Prepare a separate Thursday vendor deck",
        )
        direct = FakeModelInterpreter()
        direct.script(replace(wrong, proposals=(wrong_proposal,)))
        session.world.interpreter = direct
        session.world.workflow._model_interpreter = direct  # noqa: SLF001

        outcome = await _play(session, "msg_deadline_change", store)
        view = await engine.render(session)
        assert len(view["commitments"]) == 1
        assert outcome.headline == (
            "The agent paused for confirmation before changing anything"
        )
        assert "no commitment revision was claimed" in outcome.detail
        assert view["thread"][-1]["note"] == outcome.detail

        identity = next(
            row
            for row in view["approvals"]
            if row["requestType"] == "identity_confirmation"
        )
        await engine.resolve_approval(
            session,
            identity["approvalId"],
            {"decision": "approve"},
        )
        assert len((await engine.render(session))["commitments"]) == 2

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
        store.select_mode(session, SandboxMode.GUIDED)
        cards = {row["card_id"]: row for row in engine.available_cards(session)}
        assert cards["msg_request"]["available"]
        assert not cards["msg_accept"]["available"], "thread order is not enforced"
        assert not cards["check_in"]["available"]

        with pytest.raises(engine.SandboxCardError):
            await engine.play_card(session, "check_in")

    async def test_world_holds_no_live_surface(self, store) -> None:  # noqa: ANN001
        """The data/mutation twins have no credential, client, or controlled user."""
        world = store.create().world
        assert world.actor().user_id == SANDBOX_USER != "controlled-01"
        for attribute in ("_credentials_provider", "_client", "credentials"):
            assert not hasattr(world.calendar_writer, attribute)
            assert not hasattr(world.gmail, attribute)
        assert world.store is not None and "commitments" not in world.store
