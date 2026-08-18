"""The guided thread a judge can drive, plus its recorded interpretations.

Each guided card carries a recorded interpretation used when live
interpretation is unavailable, so the authored demonstration never dies on a
model outage; `interpreter.py` prefers the live model and falls back here.
The separate free-play lane accepts bounded judge-authored text, but never
uses these records as an answer to that text.

Recorded quotes must remain exact substrings of the message bodies — the
deterministic validator enforces that anchor on recorded and live output
alike, so an edit to a body without its quote fails the sandbox tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

PACIFIC = timezone(timedelta(hours=-7))

THREAD_ID = "sandbox-thread-vendor-comparison"
THREAD_SUBJECT = "Vendor comparison for the platform review"
FREE_PLAY_THREAD_ID = "sandbox-thread-free-play"

JORDAN = "Jordan Ellis <jordan@sandbox.invalid>"
YOU = "You <you@sandbox.invalid>"


@dataclass(frozen=True, slots=True)
class MessageCard:
    """One sendable message in the simulated thread."""

    card_id: str
    kind: Literal["message"]
    persona: Literal["jordan", "you"]
    sender: str
    label: str
    body: str
    offset_minutes: int
    recorded_wire: str
    note: str


@dataclass(frozen=True, slots=True)
class ActionCard:
    """One sendable non-message event (calendar change, time, check-in)."""

    card_id: str
    kind: Literal["conflict", "advance", "check_in"]
    label: str
    note: str


def _wire(proposals: str) -> str:
    return '{"schema_version": "extraction_v2", "proposals": [' + proposals + "]}"


def _deadline(day_offset: int, hour: int, minute: int) -> str:
    """A deadline anchored to the fixed sandbox start (Monday 09:00 Pacific)."""
    base = datetime(2026, 9, 14, 9, 0, tzinfo=PACIFIC)
    return (base + timedelta(days=day_offset)).replace(hour=hour, minute=minute).isoformat()


MESSAGE_ONE = MessageCard(
    card_id="msg_request",
    kind="message",
    persona="jordan",
    sender=JORDAN,
    label="Jordan asks you for the vendor comparison",
    body=(
        "Hi — quick ask before the platform review. Could you put together the "
        "vendor comparison deck? We need it by Friday."
    ),
    offset_minutes=0,
    recorded_wire=_wire(
        """{
          "ownership_type": "request_to_me",
          "normalized_outcome": "Put together the vendor comparison deck",
          "description": "Jordan asked for a vendor comparison deck ahead of the platform review.",
          "beneficiary_display_name": "Jordan Ellis",
          "deadline": {
            "source_expression": "by Friday",
            "proposed_value": "%s",
            "confidence": 0.88
          },
          "proposed_effort_minutes": 180,
          "identity_operation": "create",
          "target_commitment_id": null,
          "evidence": [
            {
              "message_id": "sandbox-msg-1",
              "quote": "Could you put together the vendor comparison deck?"
            }
          ],
          "confidence": 0.91
        }"""
        % _deadline(4, 17, 0)
    ),
    note=(
        "An open request addressed to you — not yet your commitment. The agent "
        "records it as a candidate and waits for your answer."
    ),
)

MESSAGE_TWO = MessageCard(
    card_id="msg_accept",
    kind="message",
    persona="you",
    sender=YOU,
    label="You accept the request",
    body=(
        "Yes, I can take that on. I'll have the vendor comparison deck to you "
        "by Friday end of day."
    ),
    offset_minutes=95,
    recorded_wire=_wire(
        """{
          "ownership_type": "my_commitment",
          "normalized_outcome": "Send the vendor comparison deck to Jordan",
          "description": "You accepted Jordan's request and committed to Friday end of day.",
          "beneficiary_display_name": "Jordan Ellis",
          "deadline": {
            "source_expression": "by Friday end of day",
            "proposed_value": "%s",
            "confidence": 0.93
          },
          "proposed_effort_minutes": 180,
          "identity_operation": "create",
          "target_commitment_id": null,
          "evidence": [
            {
              "message_id": "sandbox-msg-2",
              "quote": "I'll have the vendor comparison deck to you by Friday end of day."
            }
          ],
          "confidence": 0.95
        }"""
        % _deadline(4, 17, 0)
    ),
    note=(
        "Your acceptance converges onto the open request instead of creating a "
        "second commitment — the identity resolver recognises the same thread "
        "and outcome, and upgrades ownership to yours."
    ),
)

MESSAGE_THREE = MessageCard(
    card_id="msg_deadline_change",
    kind="message",
    persona="jordan",
    sender=JORDAN,
    label="Jordan proposes an earlier deadline",
    body=(
        "Change of plan — the review got moved up a day. Any chance you could "
        "get it to me by Thursday instead?"
    ),
    # Five minutes before the first planned block in the authored path. This
    # keeps the later conflict prospective rather than retroactively placing a
    # meeting over time that has already elapsed.
    offset_minutes=100,
    recorded_wire=_wire(
        """{
          "ownership_type": "my_commitment",
          "normalized_outcome": "Send the vendor comparison deck to Jordan",
          "description": "Jordan moved the deadline forward one day, to Thursday.",
          "beneficiary_display_name": "Jordan Ellis",
          "deadline": {
            "source_expression": "by Thursday instead",
            "proposed_value": "%s",
            "confidence": 0.9
          },
          "proposed_effort_minutes": 180,
          "identity_operation": "update_existing",
          "target_commitment_id": null,
          "evidence": [
            {
              "message_id": "sandbox-msg-3",
              "quote": "Any chance you could get it to me by Thursday instead?"
            }
          ],
          "confidence": 0.92
        }"""
        % _deadline(3, 17, 0)
    ),
    note=(
        "Jordan can propose a tighter deadline, but cannot silently rewrite "
        "the one you accepted. The agent holds Thursday for your explicit "
        "decision; accepting it revises the same commitment rather than "
        "creating a duplicate."
    ),
)

MESSAGES: tuple[MessageCard, ...] = (MESSAGE_ONE, MESSAGE_TWO, MESSAGE_THREE)

MESSAGE_IDS = {
    "msg_request": "sandbox-msg-1",
    "msg_accept": "sandbox-msg-2",
    "msg_deadline_change": "sandbox-msg-3",
}

ACTIONS: tuple[ActionCard, ...] = (
    ActionCard(
        card_id="event_conflict",
        kind="conflict",
        label="A meeting lands on top of a planned block",
        note=(
            "Someone books over your reserved time. The agent notices through "
            "the calendar change feed and repairs the plan without asking, "
            "because the move stays inside its autonomy policy."
        ),
    ),
    ActionCard(
        card_id="advance_clock",
        kind="advance",
        label="Fast-forward past the first work block",
        note=(
            "Time moves to just after your first reserved block ends. The "
            "safety reconciliation notices the block elapsed and asks you to "
            "confirm what actually happened."
        ),
    ),
    ActionCard(
        card_id="check_in",
        kind="check_in",
        label="Log 60 verified minutes on the elapsed block",
        note=(
            "Progress is only ever what you confirm. Sixty verified minutes "
            "reduce the remaining work; nothing is inferred from your calendar."
        ),
    ),
)

CARDS_BY_ID = {card.card_id: card for card in (*MESSAGES, *ACTIONS)}
