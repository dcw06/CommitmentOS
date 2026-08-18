"""Playing a card, and rendering what the agent did in response.

The engine is a thin driver: it puts an input into the world the way the
real world would deliver it (a message arrives in the mailbox, a meeting
appears on the calendar, time passes, a check-in is submitted) and then
reads the resulting durable state back out. It never decides anything —
every commitment, block, approval, and repair in the view was produced by
the production stack inside `SandboxWorld`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from commitmentos.application.commands.complete_commitment import (
    CompleteCommitmentRequest,
)
from commitmentos.application.commands.record_work_check_in import WorkCheckInRequest
from commitmentos.sandbox.scenario import (
    ACTIONS,
    CARDS_BY_ID,
    FREE_PLAY_THREAD_ID,
    JORDAN,
    MESSAGE_IDS,
    MESSAGES,
    THREAD_ID,
    THREAD_SUBJECT,
    YOU,
    ActionCard,
    MessageCard,
)
from commitmentos.sandbox.session import SandboxMode, SandboxSession
from commitmentos.sandbox.world import SANDBOX_CALENDAR_ID, SANDBOX_USER, SandboxWorld

CONFLICT_EVENT_ID = "sandbox-conflict-meeting"
CONFLICT_SUMMARY = "New meeting (sandbox experiment)"


class SandboxCardError(RuntimeError):
    """The card cannot be played from the session's current state."""


@dataclass(frozen=True, slots=True)
class CardOutcome:
    card_id: str
    headline: str
    detail: str


# ----------------------------------------------------------------------
# Playing cards
# ----------------------------------------------------------------------


async def play_card(session: SandboxSession, card_id: str) -> CardOutcome:
    card = CARDS_BY_ID.get(card_id)
    if card is None:
        raise SandboxCardError(f"unknown card {card_id}")
    if card_id in session.cards_played:
        raise SandboxCardError("card already played")
    if card_id not in {entry["card_id"] for entry in available_cards(session)}:
        raise SandboxCardError("card is not available yet")

    world = session.world
    if isinstance(card, MessageCard):
        return await _play_message(world, card)
    return await _play_action(world, card)


async def _play_message(world: SandboxWorld, card: MessageCard) -> CardOutcome:
    message_id = MESSAGE_IDS[card.card_id]
    sent_at = world.started_at + timedelta(minutes=card.offset_minutes)
    if world.clock.now() < sent_at:
        world.clock.current = sent_at
    return await _deliver_thread_message(
        world,
        message_id=message_id,
        persona=card.persona,
        sender=card.sender,
        body=card.body,
        thread_id=THREAD_ID,
        subject=THREAD_SUBJECT,
        sent_at=sent_at,
        outcome_id=card.card_id,
        guided_card_id=card.card_id,
        authored_note=card.note,
    )


async def play_custom_message(
    session: SandboxSession,
    message_id: str,
    persona: str,
    body: str,
) -> CardOutcome:
    """Deliver one judge-authored message through the real Gmail workflow."""
    if persona not in {"you", "jordan"}:
        raise SandboxCardError("unknown message sender")
    if session.mode is not SandboxMode.FREE_PLAY or not session.thread_subject:
        raise SandboxCardError("choose free play and a subject first")
    world = session.world
    latest = max(
        (message.internal_date for message in world.gmail.messages.values()),
        default=world.clock.now(),
    )
    sent_at = max(world.clock.now(), latest) + timedelta(minutes=1)
    world.clock.current = sent_at
    outcome = await _deliver_thread_message(
        world,
        message_id=message_id,
        persona=persona,
        sender=YOU if persona == "you" else JORDAN,
        body=body,
        thread_id=FREE_PLAY_THREAD_ID,
        subject=session.thread_subject,
        sent_at=sent_at,
        outcome_id=message_id,
        guided_card_id=None,
        authored_note=None,
    )
    _set_interpretation_retryability(session, message_id, "sandbox-1")
    return outcome


async def retry_custom_message(
    session: SandboxSession,
    message_id: str,
    attempt: int,
) -> CardOutcome:
    """Re-run live interpretation for one rejected message without resending it."""

    if session.mode is not SandboxMode.FREE_PLAY or not session.thread_subject:
        raise SandboxCardError("choose free play and a subject first")
    thread_message = next(
        (
            message
            for message in session.thread_messages
            if message.message_id == message_id and message.custom
        ),
        None,
    )
    gmail_message = session.world.gmail.messages.get(message_id)
    if thread_message is None or gmail_message is None:
        raise SandboxCardError("custom message is no longer available")
    producer_version = f"sandbox-retry-{attempt}"
    outcome = await _deliver_thread_message(
        session.world,
        message_id=message_id,
        persona=thread_message.persona,
        sender=thread_message.sender,
        body=thread_message.body,
        thread_id=FREE_PLAY_THREAD_ID,
        subject=thread_message.subject,
        sent_at=gmail_message.internal_date,
        outcome_id=message_id,
        guided_card_id=None,
        authored_note=None,
        add_to_mailbox=False,
        producer_version=producer_version,
    )
    _set_interpretation_retryability(session, message_id, producer_version)
    return outcome


async def _deliver_thread_message(
    world: SandboxWorld,
    *,
    message_id: str,
    persona: str,
    sender: str,
    body: str,
    thread_id: str,
    subject: str,
    sent_at: datetime,
    outcome_id: str,
    guided_card_id: str | None,
    authored_note: str | None,
    add_to_mailbox: bool = True,
    producer_version: str = "sandbox-1",
) -> CardOutcome:
    if add_to_mailbox:
        world.gmail.add_message(
            message_id=message_id,
            thread_id=thread_id,
            internal_date=sent_at,
            subject=subject,
            body_text=body,
            label_ids=("SENT",) if persona == "you" else ("INBOX",),
            headers={"from": sender},
        )
    before = _commitment_snapshot(world)
    before_approvals = _pending_approval_snapshot(world)
    interpreter = world.interpreter
    if guided_card_id is None:
        begin = getattr(interpreter, "begin_custom_message", None)
    else:
        begin = getattr(interpreter, "begin_guided_message", None)
    end = getattr(interpreter, "end_message", None)
    if callable(begin):
        begin() if guided_card_id is None else begin(guided_card_id)
    try:
        await world.deliver_message(
            message_id,
            thread_id,
            producer_version=producer_version,
        )
    finally:
        if callable(end):
            end()
    after = _commitment_snapshot(world)
    after_approvals = _pending_approval_snapshot(world)
    added = set(after) - set(before)
    changed = {
        commitment_id
        for commitment_id in set(before) & set(after)
        if before[commitment_id] != after[commitment_id]
    }
    new_approvals = set(after_approvals) - set(before_approvals)
    confirmation_paused = any(
        after_approvals[approval_id]
        in {"identity_confirmation", "deadline_change_confirmation"}
        for approval_id in new_approvals
    )
    canceled = {
        commitment_id
        for commitment_id in set(before) & set(after)
        if before[commitment_id][3] != "canceled"
        and after[commitment_id][3] == "canceled"
    }
    custom = guided_card_id is None
    if added and before:
        headline = "A separate commitment candidate was created"
        detail = (
            "This message did not revise the existing commitment. The agent "
            "created another candidate, so the divergence is shown explicitly."
        )
    elif added:
        headline = "A new commitment was extracted from the thread"
        detail = authored_note or (
            "The live interpreter found evidence in this message and the "
            "deterministic workflow created a new candidate."
        )
    elif canceled:
        headline = "The explicit retraction canceled the existing commitment"
        detail = (
            "The explicit retraction canceled the existing commitment without "
            "creating another one."
        )
    elif confirmation_paused:
        headline = "The agent paused for confirmation before changing anything"
        detail = (
            "The interpretation did not clear the deterministic identity or "
            "policy boundary, so no commitment revision was claimed."
        )
    elif changed:
        headline = "The existing commitment was revised, not duplicated"
        detail = authored_note or (
            "The message was connected to existing durable state and applied "
            "as a revision rather than a duplicate."
        )
    elif new_approvals:
        headline = "The agent paused for confirmation before changing anything"
        detail = (
            "The interpretation did not clear the deterministic identity or "
            "policy boundary, so no commitment revision was claimed."
        )
    else:
        headline = "The message produced no commitment change"
        if custom and getattr(interpreter, "last_source", "") == "custom-unavailable":
            detail = (
                "Live interpretation was unavailable or rejected this message. "
                "No canned interpretation was substituted."
            )
        else:
            detail = (
                "The thread was interpreted, but durable commitment state did not change."
            )
    return CardOutcome(outcome_id, headline, detail)


def _set_interpretation_retryability(
    session: SandboxSession,
    message_id: str,
    producer_version: str,
) -> None:
    """Offer one retry only when deterministic validation rejected this run."""

    rejected = any(
        row.get("producer_id") == f"{SANDBOX_USER}:{message_id}"
        and row.get("producer_version") == producer_version
        and row.get("reconciliation_status") == "rejected"
        for row in session.world.store.get("source_observations", {}).values()
    )
    if rejected and session.interpretation_retry_counts.get(message_id, 0) < 1:
        session.retryable_message_ids.add(message_id)
    else:
        session.retryable_message_ids.discard(message_id)


async def _play_action(world: SandboxWorld, card: ActionCard) -> CardOutcome:
    if card.kind == "conflict":
        return await _play_conflict(world, card)
    if card.kind == "advance":
        return await _play_advance(world, card)
    return await _play_check_in(world, card)


async def _play_conflict(world: SandboxWorld, card: ActionCard) -> CardOutcome:
    target = _next_planned_block(world, future_only=True)
    if target is None:
        raise SandboxCardError("no planned block to conflict with")
    block_id, block = target
    original_start = block["scheduled_start"]
    world.add_busy_event(
        CONFLICT_EVENT_ID,
        block["scheduled_start"],
        block["scheduled_end"],
        CONFLICT_SUMMARY,
    )
    # The real path: a calendar change signal, a bounded sync generation, then
    # whatever the reconciliation workflow decides to do about it.
    await world.synchronize_calendar_truth()
    moved = world.store["work_blocks"][block_id]
    if moved["scheduled_start"] != original_start:
        headline = "Conflict detected and repaired automatically"
    else:
        headline = "Conflict detected; the agent escalated instead of moving it"
    return CardOutcome(card.card_id, headline, card.note)


async def _play_advance(world: SandboxWorld, card: ActionCard) -> CardOutcome:
    blocks = _blocks(world)
    planned = [
        row
        for _, row in blocks
        if row["execution_state"] == "planned"
        and row["scheduled_end"] > world.clock.now()
    ]
    if not planned:
        raise SandboxCardError("no planned block to elapse")
    earliest_end = min(row["scheduled_end"] for row in planned)
    await world.advance_to(earliest_end + timedelta(minutes=5))
    awaiting = [
        block_id
        for block_id, row in _blocks(world)
        if row["execution_state"] == "awaiting_check_in"
    ]
    headline = (
        "A block elapsed — the agent is asking you to confirm what happened"
        if awaiting
        else "Time advanced"
    )
    return CardOutcome(card.card_id, headline, card.note)


async def _play_check_in(world: SandboxWorld, card: ActionCard) -> CardOutcome:
    awaiting = [
        (block_id, row)
        for block_id, row in _blocks(world)
        if row["execution_state"] == "awaiting_check_in"
    ]
    if not awaiting:
        raise SandboxCardError("no block is awaiting a check-in")
    block_id, row = awaiting[0]
    verified_minutes = min(60, int(row["duration_minutes"]))
    result = await world.record_work_check_in.execute(
        world.actor(),
        WorkCheckInRequest(
            work_block_id=block_id,
            idempotency_key=f"sandbox-check-in-{block_id}",
            completed=True,
            verified_minutes=verified_minutes,
            checked_in_at=world.clock.now(),
            expected_revision=row["revision"],
        ),
        "trace-sandbox-check-in",
    )
    await world.drain()
    if result.error_code:
        raise SandboxCardError(f"check-in rejected: {result.error_code}")
    return CardOutcome(
        card.card_id,
        f"{verified_minutes} verified minutes recorded against the plan",
        (
            f"Progress is only what you confirm. {verified_minutes} verified "
            "minutes reduce the remaining work; nothing is inferred from "
            "the calendar."
        ),
    )


# ----------------------------------------------------------------------
# Approvals and completion: the same guarded commands the dashboard calls
# ----------------------------------------------------------------------


async def resolve_approval(
    session: SandboxSession,
    approval_id: str,
    decision: Mapping[str, Any],
) -> CardOutcome | None:
    world = session.world
    pending = await _pending_approvals(world)
    match = next((row for row in pending if row["approval_id"] == approval_id), None)
    if match is None:
        raise SandboxCardError("approval is no longer pending")
    request_type = str(match.get("request_type") or "")
    plan_before = _plan_layout(world)
    payload = dict(decision)
    result = await world.resolve_approval.execute(
        world.actor(),
        approval_id,
        payload,
        match["revision"],
        "trace-sandbox-approval",
    )
    if result.error_code:
        raise SandboxCardError(f"approval rejected: {result.error_code}")
    # Only drain here. Synchronizing calendar truth between a plan being
    # proposed and the user approving it advances the calendar state
    # revision, which correctly stales the pending run — the planner's own
    # guard, but a confusing detour in a demonstration. Executor-triggered
    # syncs still run inside drain, after the plan is committed.
    await world.drain()
    return _deadline_resolution_outcome(
        world,
        request_type,
        str(payload.get("decision") or ""),
        plan_before,
    )


def _plan_layout(world: SandboxWorld) -> dict[str, tuple[Any, Any]]:
    return {
        block_id: (row["scheduled_start"], row["scheduled_end"])
        for block_id, row in _blocks(world)
        if row["execution_state"] in {"planned", "active", "awaiting_check_in"}
    }


def _deadline_resolution_outcome(
    world: SandboxWorld,
    request_type: str,
    decision: str,
    plan_before: Mapping[str, tuple[Any, Any]],
) -> CardOutcome | None:
    """Narrate what accepting a deadline did to the plan — including nothing.

    A preserved plan after a deadline change is minimal-change behavior worth
    stating out loud, not silence a judge has to interpret.
    """
    if request_type != "deadline_change_confirmation" or decision != "approve":
        return None
    plan_after = _plan_layout(world)
    if not plan_before and not plan_after:
        return None
    if plan_after == dict(plan_before):
        return CardOutcome(
            "deadline_change_confirmation",
            "Deadline accepted; the existing plan was preserved",
            (
                "Deadline accepted. Feasibility was recalculated; all existing "
                "blocks remained valid, so the plan was preserved unchanged."
            ),
        )
    return CardOutcome(
        "deadline_change_confirmation",
        "Deadline accepted; the plan was revised",
        (
            "Deadline accepted. Feasibility was recalculated against the new "
            "deadline and the existing plan no longer fit, so the calendar "
            "shows the revised blocks."
        ),
    )


async def complete_commitment(session: SandboxSession, commitment_id: str) -> None:
    world = session.world
    document = world.store.get("commitments", {}).get(commitment_id)
    if document is None:
        raise SandboxCardError("unknown commitment")
    result = await world.complete_commitment.execute(
        world.actor(),
        CompleteCommitmentRequest(
            commitment_id=commitment_id,
            idempotency_key=f"sandbox-complete-{commitment_id}",
            completed_at=world.clock.now(),
            expected_revision=document["revision"],
            note=f"Completed: {document.get('title', 'sandbox commitment')}.",
        ),
        "trace-sandbox-complete",
    )
    if result.error_code:
        raise SandboxCardError(f"completion rejected: {result.error_code}")
    await world.drain()


# ----------------------------------------------------------------------
# The view
# ----------------------------------------------------------------------


async def render(session: SandboxSession) -> dict[str, Any]:
    world = session.world
    approvals = await _pending_approvals(world)
    return {
        "sessionId": session.session_id,
        "mode": session.mode.value,
        "threadSubject": session.thread_subject,
        "customMessagesRemaining": max(
            session.custom_message_limit - session.custom_messages_sent,
            0,
        ),
        "now": world.clock.now().isoformat(),
        "interpretationSource": session.world.interpreter.last_source
        if hasattr(session.world.interpreter, "last_source")
        else "recorded",
        "thread": _thread_view(session),
        "cards": available_cards(session),
        "commitments": _commitments_view(world),
        "blocks": _blocks_view(world),
        "approvals": [_approval_view(world, row) for row in approvals],
        "activity": _activity_view(world),
        "calendar": _calendar_view(world),
    }


def available_cards(session: SandboxSession) -> list[dict[str, Any]]:
    """Which cards can be played next, and why the others cannot.

    The deck is ordered: the thread advances one message at a time, a
    conflict needs a planned block to land on, a check-in needs an elapsed
    one. Rather than hide unavailable cards, each carries the reason it is
    waiting, so a judge can see what the system is waiting for.
    """
    world = session.world
    if session.mode is SandboxMode.UNSELECTED:
        return []
    if session.mode is SandboxMode.FREE_PLAY:
        return _free_play_action_cards(session)
    played = set(session.cards_played)
    entries: list[dict[str, Any]] = []
    pending_approval = _has_pending_approvals(world)
    has_planned = _next_planned_block(world) is not None

    next_message = next((card for card in MESSAGES if card.card_id not in played), None)
    for card in MESSAGES:
        if card.card_id in played:
            continue
        is_next = next_message is not None and card.card_id == next_message.card_id
        reason = None if is_next else "Send the earlier message first"
        if is_next and card.card_id == "msg_deadline_change":
            if pending_approval:
                is_next = False
                reason = "Resolve the pending confirmation first"
            elif not has_planned:
                is_next = False
                reason = "Confirm the effort and approve the first plan"
        entries.append(
            {
                "card_id": card.card_id,
                "kind": "message",
                "persona": card.persona,
                "label": card.label,
                "body": card.body,
                "available": is_next,
                "blocked_reason": None if is_next else reason,
            }
        )

    has_awaiting = any(
        row["execution_state"] == "awaiting_check_in" for _, row in _blocks(world)
    )
    messages_complete = all(card.card_id in played for card in MESSAGES)
    conflict_played = "event_conflict" in played
    advance_played = "advance_clock" in played
    for card in ACTIONS:
        if card.card_id in played:
            continue
        if card.kind == "conflict":
            available = messages_complete and not pending_approval and has_planned
            if not messages_complete:
                reason = "Finish the thread first"
            elif pending_approval:
                reason = "Resolve the pending confirmation first"
            else:
                reason = "There is no future reserved block to conflict with"
        elif card.kind == "advance":
            available = conflict_played and not pending_approval and has_planned
            if not conflict_played:
                reason = "Create the calendar conflict first"
            elif pending_approval:
                reason = "Resolve the pending confirmation first"
            else:
                reason = "There is no reserved block to elapse yet"
        else:
            available = advance_played and not pending_approval and has_awaiting
            if not advance_played:
                reason = "Fast-forward past a block first"
            elif pending_approval:
                reason = "Resolve the pending confirmation first"
            else:
                reason = "No block is awaiting a check-in"
        entries.append(
            {
                "card_id": card.card_id,
                "kind": card.kind,
                "persona": None,
                "label": card.label,
                "body": None,
                "available": available,
                "blocked_reason": None if available else reason,
            }
        )
    return entries


def _free_play_action_cards(session: SandboxSession) -> list[dict[str, Any]]:
    """Expose the real repair/check-in loop once free-play creates a plan."""

    world = session.world
    played = set(session.cards_played)
    pending_approval = _has_pending_approvals(world)
    has_planned = _next_planned_block(world) is not None
    has_awaiting = any(
        row["execution_state"] == "awaiting_check_in" for _, row in _blocks(world)
    )
    conflict_played = "event_conflict" in played
    advance_played = "advance_clock" in played
    awaiting_duration = next(
        (
            int(row["duration_minutes"])
            for _, row in _blocks(world)
            if row["execution_state"] == "awaiting_check_in"
        ),
        None,
    )

    entries: list[dict[str, Any]] = []
    for card in ACTIONS:
        if card.card_id in played:
            continue
        label = card.label
        if card.kind == "conflict":
            available = not pending_approval and has_planned
            reason = (
                "Resolve the pending confirmation first"
                if pending_approval
                else "Create and approve a plan before testing a conflict"
            )
        elif card.kind == "advance":
            available = conflict_played and not pending_approval and has_planned
            if not conflict_played:
                reason = "Create the calendar conflict first"
            elif pending_approval:
                reason = "Resolve the pending confirmation first"
            else:
                reason = "There is no reserved block to elapse yet"
        else:
            available = advance_played and not pending_approval and has_awaiting
            if awaiting_duration is not None:
                label = (
                    f"Log {min(60, awaiting_duration)} verified minutes "
                    "on the elapsed block"
                )
            if not advance_played:
                reason = "Fast-forward past a block first"
            elif pending_approval:
                reason = "Resolve the pending confirmation first"
            else:
                reason = "No block is awaiting a check-in"
        entries.append(
            {
                "card_id": card.card_id,
                "kind": card.kind,
                "persona": None,
                "label": label,
                "body": None,
                "available": available,
                "blocked_reason": None if available else reason,
            }
        )
    return entries


def _thread_view(session: SandboxSession) -> list[dict[str, Any]]:
    return [
        {
            "card_id": message.message_id,
            "persona": message.persona,
            "sender": message.sender,
            "subject": message.subject,
            "body": message.body,
            "note": _current_thread_note(session, message.message_id, message.note),
            "custom": message.custom,
            "retryAvailable": message.message_id in session.retryable_message_ids,
            "retryAttemptsRemaining": (
                max(1 - session.interpretation_retry_counts.get(message.message_id, 0), 0)
                if message.custom
                else 0
            ),
        }
        for message in session.thread_messages
    ]


def _current_thread_note(
    session: SandboxSession,
    rendered_message_id: str,
    original_note: str,
) -> str:
    source_message_id = MESSAGE_IDS.get(rendered_message_id, rendered_message_id)
    for approval in session.world.store.get("approvals", {}).values():
        request_type = approval.get("request_type")
        if request_type not in {
            "deadline_change_confirmation",
            "retraction_confirmation",
        }:
            continue
        source_reference = (approval.get("payload") or {}).get(
            "source_reference"
        ) or {}
        if source_reference.get("message_id") != source_message_id:
            continue
        decision = (approval.get("decision") or {}).get("decision")
        if request_type == "deadline_change_confirmation":
            if decision == "approve":
                return "The proposal was held for confirmation and later accepted by you."
            if decision == "reject":
                return (
                    "The proposal was held for confirmation and later rejected by you; "
                    "the binding deadline did not change."
                )
        if request_type == "retraction_confirmation":
            if decision == "approve":
                return (
                    "The retraction was held for confirmation and later accepted by you; "
                    "the existing commitment was canceled."
                )
            if decision == "reject":
                return (
                    "The retraction was held for confirmation and later rejected by you; "
                    "the existing commitment stayed open."
                )
    return original_note


def _commitments_view(world: SandboxWorld) -> list[dict[str, Any]]:
    rows = []
    for commitment_id, document in sorted(
        world.store.get("commitments", {}).items(), key=lambda item: item[1]["created_at"]
    ):
        projection = document.get("projection") or {}
        effort = document.get("effort") or {}
        deadline = document.get("deadline") or {}
        terminal = document["lifecycle_status"] in {
            "completed",
            "dismissed",
            "canceled",
        }
        verified_minutes = sum(
            int(block.get("verified_minutes") or 0)
            for _, block in _blocks(world)
            if block.get("commitment_id") == commitment_id
        )
        remaining_minutes = projection.get("remaining_minutes")
        if remaining_minutes is None and effort.get("confirmed_minutes") is not None:
            remaining_minutes = max(
                int(effort["confirmed_minutes"]) - verified_minutes,
                0,
            )
        rows.append(
            {
                "commitmentId": commitment_id,
                "title": document["title"],
                "ownershipType": document["ownership_type"],
                "lifecycleStatus": document["lifecycle_status"],
                "pendingStage": _pending_stage(world, commitment_id, document),
                "revision": document["revision"],
                "deadline": _iso(deadline.get("value")),
                "deadlineExpression": deadline.get("source_expression"),
                "deadlineConfidence": deadline.get("confidence"),
                "proposedMinutes": effort.get("proposed_minutes"),
                "confirmedMinutes": effort.get("confirmed_minutes"),
                "verifiedMinutes": verified_minutes,
                "remainingMinutes": (
                    None if terminal else remaining_minutes
                ),
                "riskLevel": None if terminal else projection.get("risk_level"),
                "evidence": _evidence_view(
                    world,
                    commitment_id,
                    str(deadline.get("evidence_id") or ""),
                ),
            }
        )
    return rows


def _pending_stage(
    world: SandboxWorld,
    commitment_id: str,
    document: Mapping[str, Any],
) -> str | None:
    """Say which confirmation an awaiting commitment is actually waiting on.

    "Awaiting confirmation" covers two different boundaries — the effort
    number and the first calendar write. The distinction is real state (two
    approval types), so surface it instead of one umbrella label.
    """
    if document.get("lifecycle_status") != "awaiting_confirmation":
        return None
    stages = {
        "effort_confirmation": "awaiting_effort_confirmation",
        "initial_plan_approval": "awaiting_plan_approval",
    }
    for approval in world.store.get("approvals", {}).values():
        if approval.get("commitment_id") != commitment_id:
            continue
        if approval.get("status") != "pending":
            continue
        stage = stages.get(str(approval.get("request_type")))
        if stage is not None:
            return stage
    return None


def _evidence_view(
    world: SandboxWorld,
    commitment_id: str,
    deadline_evidence_id: str,
) -> list[dict[str, Any]]:
    rows = []
    seen: set[tuple[str, str]] = set()
    evidence_documents = sorted(
        world.store.get("evidence", {}).items(),
        key=lambda item: (
            item[0] == deadline_evidence_id,
            _iso(item[1].get("created_at") or item[1].get("recorded_at")) or "",
        ),
        reverse=True,
    )
    for evidence_id, document in evidence_documents:
        if document.get("commitment_id") != commitment_id:
            continue
        excerpt = document.get("excerpt") or document.get("note") or ""
        normalized_excerpt = " ".join(str(excerpt).split()).casefold()
        if not normalized_excerpt:
            continue
        source_reference = document.get("source_reference") or {}
        source_key = str(source_reference.get("message_id") or evidence_id)
        dedupe_key = (source_key, normalized_excerpt)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rows.append(
            {
                "excerpt": excerpt,
                "kind": document.get("evidence_type") or document.get("kind"),
                "supportsDeadline": evidence_id == deadline_evidence_id,
                "createdAt": _iso(
                    document.get("created_at") or document.get("recorded_at")
                ),
            }
        )
    rows.sort(
        key=lambda row: (bool(row["supportsDeadline"]), row["createdAt"] or ""),
        reverse=True,
    )
    return rows[:4]


def _blocks_view(world: SandboxWorld) -> list[dict[str, Any]]:
    rows = []
    for block_id, document in _blocks(world):
        commitment = world.store.get("commitments", {}).get(document["commitment_id"], {})
        rows.append(
            {
                "workBlockId": block_id,
                "commitmentId": document["commitment_id"],
                "title": commitment.get("title", "Work block"),
                "start": _iso(document["scheduled_start"]),
                "end": _iso(document["scheduled_end"]),
                "durationMinutes": document["duration_minutes"],
                "executionState": document["execution_state"],
                "verifiedMinutes": document["verified_minutes"],
                "calendarEventId": document["calendar_event_id"],
                "planRevision": document.get("plan_revision"),
            }
        )
    return rows


def _calendar_view(world: SandboxWorld) -> list[dict[str, Any]]:
    owned_ids = {
        document["calendar_event_id"] for _, document in _blocks(world)
    }
    rows = []
    for (calendar_id, event_id), event in world.calendar.events.items():
        if calendar_id != SANDBOX_CALENDAR_ID or event.get("status") == "cancelled":
            continue
        if event_id in owned_ids:
            continue
        rows.append(
            {
                "eventId": event_id,
                "summary": event.get("summary", "Busy"),
                "start": _iso(event["start"]),
                "end": _iso(event["end"]),
            }
        )
    return sorted(rows, key=lambda row: row["start"])


def _approval_view(world: SandboxWorld, row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") or {}
    proposal = payload.get("proposal") or {}
    proposed_minutes = payload.get("proposed_minutes")
    if proposed_minutes is None:
        proposed_minutes = proposal.get("proposed_effort_minutes")
    if proposed_minutes is None:
        proposed_minutes = payload.get("proposed_effort_minutes")
    proposed_ownership = proposal.get("ownership_type") or payload.get(
        "ownership_type"
    )
    requires_ownership = (
        row["request_type"] == "identity_confirmation"
        and proposed_ownership == "ambiguous"
    )
    commitment = world.store.get("commitments", {}).get(
        str(row.get("commitment_id") or ""),
        {},
    )
    return {
        "approvalId": row["approval_id"],
        "requestType": row["request_type"],
        "commitmentId": row.get("commitment_id"),
        "commitmentTitle": commitment.get("title")
        or proposal.get("normalized_outcome")
        or payload.get("normalized_outcome"),
        "revision": row["revision"],
        "reason": row.get("policy_reason")
        or payload.get("policy_reason")
        or payload.get("reason"),
        "previousRejectionReason": payload.get("previous_rejection_reason"),
        "proposedMinutes": proposed_minutes,
        "options": list(payload.get("options") or ()),
        "requiresOwnership": requires_ownership,
        "ownershipOptions": (
            ["my_commitment", "request_to_me", "commitment_to_me"]
            if requires_ownership
            else []
        ),
        "proposedOwnership": proposed_ownership,
        "proposedOperation": proposal.get("proposed_identity_operation")
        or payload.get("proposed_operation"),
        "normalizedOutcome": proposal.get("normalized_outcome")
        or payload.get("normalized_outcome"),
        "proposedDeadline": (proposal.get("deadline") or {}).get("proposed_value"),
        "proposedDeadlineExpression": (proposal.get("deadline") or {}).get(
            "source_expression"
        ),
    }


def _activity_view(world: SandboxWorld) -> list[dict[str, Any]]:
    events = [
        {**document, "_id": event_id}
        for event_id, document in world.store.get("activity_events", {}).items()
        if document.get("user_id") == SANDBOX_USER
    ]
    events.sort(key=lambda row: row["created_at"])
    return [
        {
            "eventId": row["_id"],
            "eventType": row["event_type"],
            "summary": row.get("summary", ""),
            "createdAt": _iso(row["created_at"]),
            "actor": row.get("actor"),
            "correlationId": row.get("trace_id"),
            "evidence": _evidence_projection(world, row),
        }
        for row in events
    ][-40:]


# The audit evidence projection. Every field a judge can expand is copied
# key-by-key from this explicit allowlist — the raw payload is never spread
# into the response, so evidence excerpts, model input/output, headers, and
# provider response bodies are excluded by construction, not by filtering.
_EVIDENCE_PAYLOAD_FIELDS: tuple[tuple[str, str], ...] = (
    ("observation_id", "observationId"),
    ("source_observation_id", "sourceObservationId"),
    ("commitment_revision", "commitmentRevision"),
    ("plan_revision", "planRevision"),
    ("policy_reason", "policyReason"),
    ("calendar_event_id", "stableEventId"),
    ("work_block_id", "workBlockId"),
    ("execution_status", "outboxStatus"),
    ("moved_block_count", "movedBlockCount"),
    ("repair_latency_ms", "repairLatencyMs"),
    ("decision_latency_ms", "decisionLatencyMs"),
)

_EVIDENCE_ACTION_LIMIT = 6


def _evidence_projection(
    world: SandboxWorld, row: Mapping[str, Any]
) -> dict[str, Any]:
    payload = row.get("payload") or {}
    evidence: dict[str, Any] = {}
    for source_key, target_key in _EVIDENCE_PAYLOAD_FIELDS:
        value = payload.get(source_key)
        if value is not None and target_key not in evidence:
            evidence[target_key] = value
    preserved = payload.get("preserved_work_block_ids")
    if isinstance(preserved, (list, tuple)):
        evidence["preservedBlockCount"] = len(preserved)
    planner_run_id = payload.get("planner_run_id")
    if planner_run_id:
        evidence["plannerRunId"] = str(planner_run_id)
        run = world.store.get("planner_runs", {}).get(str(planner_run_id))
        if run is not None and run.get("planner_version"):
            evidence["plannerVersion"] = str(run["planner_version"])
    actions = _outbox_evidence(world, payload)
    if actions:
        evidence["outboxActions"] = actions
    return evidence


def _outbox_evidence(
    world: SandboxWorld, payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    outbox_ids = payload.get("outbox_ids")
    if not isinstance(outbox_ids, (list, tuple)):
        outbox_ids = [payload["outbox_id"]] if payload.get("outbox_id") else []
    actions: list[dict[str, Any]] = []
    for outbox_id in list(outbox_ids)[:_EVIDENCE_ACTION_LIMIT]:
        document = world.store.get("action_outbox", {}).get(str(outbox_id))
        if document is None:
            continue
        mutation = document.get("mutation") or {}
        response = document.get("mutation_response") or {}
        action = {
            "outboxId": str(outbox_id),
            "idempotencyKey": document.get("action_idempotency_key"),
            "outboxStatus": document.get("execution_status"),
            "actionType": mutation.get("action_type"),
            "stableEventId": mutation.get("calendar_event_id"),
            "expectedEtag": mutation.get("expected_observed_event_etag"),
            "observedEtag": response.get("mutation_response_etag"),
        }
        actions.append(
            {key: value for key, value in action.items() if value is not None}
        )
    return actions


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _blocks(world: SandboxWorld) -> list[tuple[str, dict[str, Any]]]:
    return sorted(
        world.store.get("work_blocks", {}).items(),
        key=lambda item: item[1]["scheduled_start"],
    )


def _next_planned_block(
    world: SandboxWorld, *, future_only: bool = False
) -> tuple[str, dict[str, Any]] | None:
    for block_id, document in _blocks(world):
        if document["execution_state"] != "planned":
            continue
        if document["scheduled_end"] <= world.clock.now():
            continue
        if future_only and document["scheduled_start"] < world.clock.now():
            continue
        return block_id, document
    return None


def _commitment_snapshot(world: SandboxWorld) -> dict[str, tuple[Any, ...]]:
    """Fields that make a message a user-visible semantic revision.

    Internal reconciliation metadata can advance a document revision even when
    identity resolution pauses. It must not make the sandbox claim that the
    message's ownership, outcome, or deadline was applied.
    """
    return {
        commitment_id: (
            document.get("title"),
            (document.get("deadline") or {}).get("value"),
            document.get("ownership_type"),
            document.get("lifecycle_status"),
        )
        for commitment_id, document in world.store.get("commitments", {}).items()
    }


def _has_pending_approvals(
    world: SandboxWorld, request_type: str | None = None
) -> bool:
    return any(
        document.get("user_id") == SANDBOX_USER
        and document.get("status") == "pending"
        and (request_type is None or document.get("request_type") == request_type)
        for document in world.store.get("approvals", {}).values()
    )


def _pending_approval_snapshot(world: SandboxWorld) -> dict[str, str]:
    return {
        approval_id: str(document.get("request_type", ""))
        for approval_id, document in world.store.get("approvals", {}).items()
        if document.get("user_id") == SANDBOX_USER
        and document.get("status") == "pending"
    }


async def _pending_approvals(world: SandboxWorld) -> list[dict[str, Any]]:
    async def _load(repositories):  # noqa: ANN001, ANN202
        return list(await repositories.approvals.list_pending(SANDBOX_USER))

    return await world.uow.read(_load)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if value is None else str(value)
