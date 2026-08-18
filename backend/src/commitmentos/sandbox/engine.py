"""Playing a card, and rendering what the agent did in response.

The engine is a thin driver: it puts an input into the world the way the
real world would deliver it (a message arrives in the mailbox, a meeting
appears on the calendar, time passes, a check-in is submitted) and then
reads the resulting durable state back out. It never decides anything —
every commitment, block, approval, and repair in the view was produced by
the production stack inside `SandboxWorld`.
"""

from __future__ import annotations

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
    MESSAGE_IDS,
    MESSAGES,
    THREAD_ID,
    THREAD_SUBJECT,
    ActionCard,
    MessageCard,
)
from commitmentos.sandbox.session import SandboxSession
from commitmentos.sandbox.world import SANDBOX_CALENDAR_ID, SANDBOX_USER, SandboxWorld

CONFLICT_EVENT_ID = "sandbox-conflict-meeting"
CONFLICT_SUMMARY = "Platform review prep (booked by someone else)"


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
    world.gmail.add_message(
        message_id=message_id,
        thread_id=THREAD_ID,
        internal_date=sent_at,
        subject=THREAD_SUBJECT,
        body_text=card.body,
        label_ids=("SENT",) if card.persona == "you" else ("INBOX",),
        headers={"from": card.sender},
    )
    before = _commitment_count(world)
    await world.deliver_message(message_id, THREAD_ID)
    after = _commitment_count(world)
    if after > before:
        headline = "A new commitment was extracted from the thread"
    elif after == before and before > 0:
        headline = "The existing commitment was revised, not duplicated"
    else:
        headline = "The message was interpreted"
    return CardOutcome(card.card_id, headline, card.note)


async def _play_action(world: SandboxWorld, card: ActionCard) -> CardOutcome:
    if card.kind == "conflict":
        return await _play_conflict(world, card)
    if card.kind == "advance":
        return await _play_advance(world, card)
    return await _play_check_in(world, card)


async def _play_conflict(world: SandboxWorld, card: ActionCard) -> CardOutcome:
    target = _next_planned_block(world)
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
    planned = [row for _, row in blocks if row["execution_state"] == "planned"]
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
    result = await world.record_work_check_in.execute(
        world.actor(),
        WorkCheckInRequest(
            work_block_id=block_id,
            idempotency_key=f"sandbox-check-in-{block_id}",
            completed=True,
            verified_minutes=60,
            checked_in_at=world.clock.now(),
            expected_revision=row["revision"],
        ),
        "trace-sandbox-check-in",
    )
    await world.drain()
    if result.error_code:
        raise SandboxCardError(f"check-in rejected: {result.error_code}")
    return CardOutcome(
        card.card_id, "60 verified minutes recorded against the plan", card.note
    )


# ----------------------------------------------------------------------
# Approvals and completion: the same guarded commands the dashboard calls
# ----------------------------------------------------------------------


async def resolve_approval(
    session: SandboxSession,
    approval_id: str,
    decision: str,
    confirmed_minutes: int | None,
) -> None:
    world = session.world
    pending = await _pending_approvals(world)
    match = next((row for row in pending if row["approval_id"] == approval_id), None)
    if match is None:
        raise SandboxCardError("approval is no longer pending")
    payload: dict[str, Any] = {"decision": decision}
    if confirmed_minutes is not None:
        payload["confirmed_minutes"] = confirmed_minutes
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
            note="Sent the vendor comparison deck to Jordan.",
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
        "now": world.clock.now().isoformat(),
        "interpretationSource": session.world.interpreter.last_source
        if hasattr(session.world.interpreter, "last_source")
        else "recorded",
        "thread": _thread_view(session),
        "cards": available_cards(session),
        "commitments": _commitments_view(world),
        "blocks": _blocks_view(world),
        "approvals": [_approval_view(row) for row in approvals],
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
    played = set(session.cards_played)
    entries: list[dict[str, Any]] = []

    next_message = next((card for card in MESSAGES if card.card_id not in played), None)
    for card in MESSAGES:
        if card.card_id in played:
            continue
        is_next = next_message is not None and card.card_id == next_message.card_id
        entries.append(
            {
                "card_id": card.card_id,
                "kind": "message",
                "persona": card.persona,
                "label": card.label,
                "body": card.body,
                "available": is_next,
                "blocked_reason": None if is_next else "Send the earlier message first",
            }
        )

    has_planned = _next_planned_block(world) is not None
    has_awaiting = any(
        row["execution_state"] == "awaiting_check_in" for _, row in _blocks(world)
    )
    conflict_played = "event_conflict" in played
    for card in ACTIONS:
        if card.card_id in played:
            continue
        if card.kind == "conflict":
            available, reason = has_planned, "Approve a plan first — there is nothing to conflict with"
        elif card.kind == "advance":
            available, reason = has_planned, "There is no reserved block to elapse yet"
        else:
            available, reason = has_awaiting, "Fast-forward past a block first"
        if card.kind == "advance" and has_planned and not conflict_played:
            reason = None
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


def _thread_view(session: SandboxSession) -> list[dict[str, Any]]:
    played = set(session.cards_played)
    messages = []
    for card in MESSAGES:
        if card.card_id not in played:
            continue
        messages.append(
            {
                "card_id": card.card_id,
                "persona": card.persona,
                "sender": "Jordan Ellis" if card.persona == "jordan" else "You",
                "body": card.body,
                "note": card.note,
            }
        )
    return messages


def _commitments_view(world: SandboxWorld) -> list[dict[str, Any]]:
    rows = []
    for commitment_id, document in sorted(
        world.store.get("commitments", {}).items(), key=lambda item: item[1]["created_at"]
    ):
        projection = document.get("projection") or {}
        effort = document.get("effort") or {}
        deadline = document.get("deadline") or {}
        rows.append(
            {
                "commitmentId": commitment_id,
                "title": document["title"],
                "ownershipType": document["ownership_type"],
                "lifecycleStatus": document["lifecycle_status"],
                "revision": document["revision"],
                "deadline": _iso(deadline.get("value")),
                "deadlineExpression": deadline.get("source_expression"),
                "deadlineConfidence": deadline.get("confidence"),
                "proposedMinutes": effort.get("proposed_minutes"),
                "confirmedMinutes": effort.get("confirmed_minutes"),
                "verifiedMinutes": projection.get("verified_completed_minutes"),
                "remainingMinutes": projection.get("remaining_minutes"),
                "riskLevel": projection.get("risk_level"),
                "evidence": _evidence_view(world, commitment_id),
            }
        )
    return rows


def _evidence_view(world: SandboxWorld, commitment_id: str) -> list[dict[str, Any]]:
    rows = []
    for document in world.store.get("evidence", {}).values():
        if document.get("commitment_id") != commitment_id:
            continue
        rows.append(
            {
                "excerpt": document.get("excerpt", ""),
                "kind": document.get("evidence_type") or document.get("kind"),
            }
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


def _approval_view(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") or {}
    proposal = payload.get("proposal") or {}
    return {
        "approvalId": row["approval_id"],
        "requestType": row["request_type"],
        "commitmentId": row.get("commitment_id"),
        "revision": row["revision"],
        "reason": payload.get("policy_reason") or payload.get("reason"),
        "proposedMinutes": proposal.get("proposed_effort_minutes")
        or payload.get("proposed_effort_minutes"),
        "options": list(payload.get("options") or ()),
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
        }
        for row in events
    ][-40:]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _blocks(world: SandboxWorld) -> list[tuple[str, dict[str, Any]]]:
    return sorted(
        world.store.get("work_blocks", {}).items(),
        key=lambda item: item[1]["scheduled_start"],
    )


def _next_planned_block(world: SandboxWorld) -> tuple[str, dict[str, Any]] | None:
    for block_id, document in _blocks(world):
        if document["execution_state"] == "planned":
            return block_id, document
    return None


def _commitment_count(world: SandboxWorld) -> int:
    return len(world.store.get("commitments", {}))


async def _pending_approvals(world: SandboxWorld) -> list[dict[str, Any]]:
    async def _load(repositories):  # noqa: ANN001, ANN202
        return list(await repositories.approvals.list_pending(SANDBOX_USER))

    return await world.uow.read(_load)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if value is None else str(value)
