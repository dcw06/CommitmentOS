"""Per-judge sandbox sessions: isolated, capped, and self-expiring.

Each browser gets its own `SandboxWorld` keyed by an opaque session id. The
store is deliberately process-local: sandbox state is a demonstration
artifact, never durable truth, so a Cloud Run instance recycle simply drops
it and the judge starts a fresh story. Caps bound what a public surface can
cost — a maximum number of concurrent worlds, creation rate, idle and absolute
expiry, per-session actions, and rolling request/model-call limits. Read-only
polling never extends a world's lifetime.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from commitmentos.application.ports.model_interpreter import ModelInterpreter
from commitmentos.sandbox.interpreter import (
    BoundedSandboxModelInterpreter,
    SandboxInterpretationCache,
    SandboxInterpreter,
    SandboxModelCallGate,
)
from commitmentos.sandbox.scenario import CARDS_BY_ID, THREAD_SUBJECT, MessageCard
from commitmentos.sandbox.world import SandboxWorld, default_start

MAX_SESSIONS = 40
IDLE_EXPIRY = timedelta(minutes=45)
ABSOLUTE_EXPIRY = timedelta(hours=2)
MAX_CARDS_PER_SESSION = 60
MAX_CUSTOM_MESSAGES_PER_SESSION = 8
MAX_CUSTOM_MESSAGES_PER_WINDOW = 12
MAX_INTERPRETATION_RETRIES_PER_SESSION = 3
MAX_INTERPRETATION_RETRIES_PER_MESSAGE = 1
CUSTOM_MESSAGE_WINDOW = timedelta(minutes=1)
MAX_SESSION_CREATIONS_PER_WINDOW = 12
SESSION_CREATION_WINDOW = timedelta(minutes=1)


class SandboxMode(StrEnum):
    UNSELECTED = "unselected"
    GUIDED = "guided"
    FREE_PLAY = "free_play"


class SandboxCapacityError(RuntimeError):
    """The sandbox is at its concurrent-session ceiling."""


class SandboxBudgetError(RuntimeError):
    """This session has spent its card budget."""


class SandboxCustomMessageBudgetError(RuntimeError):
    """This session has spent its free-play message budget."""


class SandboxCustomMessageRateError(RuntimeError):
    """The process-wide rolling free-play ceiling has been reached."""


class SandboxInterpretationRetryError(RuntimeError):
    """The selected message is not eligible for another interpretation retry."""


class SandboxSessionRateError(RuntimeError):
    """New public worlds are being opened too quickly."""


class SandboxModeError(RuntimeError):
    """An action belongs to the other mutually isolated sandbox lane."""


@dataclass(frozen=True, slots=True)
class SandboxThreadMessage:
    message_id: str
    persona: str
    sender: str
    subject: str
    body: str
    note: str
    custom: bool


@dataclass
class SandboxSession:
    session_id: str
    world: SandboxWorld
    created_at: datetime
    last_activity_at: datetime
    custom_message_limit: int = MAX_CUSTOM_MESSAGES_PER_SESSION
    mode: SandboxMode = SandboxMode.UNSELECTED
    thread_subject: str | None = None
    cards_played: list[str] = field(default_factory=list)
    card_notes: dict[str, str] = field(default_factory=dict)
    thread_messages: list[SandboxThreadMessage] = field(default_factory=list)
    custom_messages_sent: int = 0
    interpretation_retries_sent: int = 0
    interpretation_retry_counts: dict[str, int] = field(default_factory=dict)
    retryable_message_ids: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class SandboxSessionStore:
    def __init__(
        self,
        live_interpreter: ModelInterpreter | None,
        *,
        max_sessions: int = MAX_SESSIONS,
        idle_expiry: timedelta = IDLE_EXPIRY,
        absolute_expiry: timedelta = ABSOLUTE_EXPIRY,
        max_custom_messages_per_session: int = MAX_CUSTOM_MESSAGES_PER_SESSION,
        max_custom_messages_per_window: int = MAX_CUSTOM_MESSAGES_PER_WINDOW,
        custom_message_window: timedelta = CUSTOM_MESSAGE_WINDOW,
        max_session_creations_per_window: int = MAX_SESSION_CREATIONS_PER_WINDOW,
        session_creation_window: timedelta = SESSION_CREATION_WINDOW,
        model_call_gate: SandboxModelCallGate | None = None,
    ) -> None:
        self._sessions: dict[str, SandboxSession] = {}
        # Results are shared, while provenance remains on the per-world
        # interpreter so one judge can never relabel another judge's session.
        self._live_interpreter = live_interpreter
        self._interpretation_cache = SandboxInterpretationCache()
        self._model_call_gate = model_call_gate or SandboxModelCallGate()
        self._max_sessions = max_sessions
        self._idle_expiry = idle_expiry
        self._absolute_expiry = absolute_expiry
        self._max_custom_messages_per_session = max_custom_messages_per_session
        self._max_custom_messages_per_window = max_custom_messages_per_window
        self._custom_message_window = custom_message_window
        self._custom_message_times: deque[datetime] = deque()
        self._max_session_creations_per_window = max_session_creations_per_window
        self._session_creation_window = session_creation_window
        self._session_creation_times: deque[datetime] = deque()
        self._expiry_handles: dict[str, asyncio.TimerHandle] = {}
        self._guard = threading.RLock()

    @property
    def interpreter(self) -> SandboxInterpreter:
        """Compatibility view for diagnostics; sessions own the active wrappers."""
        return self._new_interpreter()

    def _new_interpreter(self) -> SandboxInterpreter:
        live = (
            BoundedSandboxModelInterpreter(
                self._live_interpreter, self._model_call_gate
            )
            if self._live_interpreter is not None
            else None
        )
        return SandboxInterpreter(live, self._interpretation_cache)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _evict_expired(self) -> None:
        now = self._now()
        idle_cutoff = now - self._idle_expiry
        absolute_cutoff = now - self._absolute_expiry
        for session_id in [
            key
            for key, value in self._sessions.items()
            if value.last_activity_at <= idle_cutoff
            or value.created_at <= absolute_cutoff
        ]:
            self._drop_locked(session_id)

    def _drop_locked(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        handle = self._expiry_handles.pop(session_id, None)
        if handle is not None:
            handle.cancel()

    def _schedule_expiry_locked(self, session: SandboxSession) -> None:
        """Forget private session text even if no later request runs cleanup."""

        previous = self._expiry_handles.pop(session.session_id, None)
        if previous is not None:
            previous.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous unit tests still exercise lazy eviction through get;
            # the ASGI service always creates and touches sessions on its loop.
            return
        expires_at = min(
            session.last_activity_at + self._idle_expiry,
            session.created_at + self._absolute_expiry,
        )
        delay = max(0.0, (expires_at - self._now()).total_seconds())
        self._expiry_handles[session.session_id] = loop.call_later(
            delay,
            self._expire_due,
            session.session_id,
        )

    def _expire_due(self, session_id: str) -> None:
        with self._guard:
            session = self._sessions.get(session_id)
            if session is None:
                self._drop_locked(session_id)
                return
            expires_at = min(
                session.last_activity_at + self._idle_expiry,
                session.created_at + self._absolute_expiry,
            )
            if expires_at <= self._now():
                self._drop_locked(session_id)
            else:
                self._schedule_expiry_locked(session)

    def create(self) -> SandboxSession:
        with self._guard:
            return self._create_locked(charge_creation=True)

    def _create_locked(self, *, charge_creation: bool) -> SandboxSession:
        self._evict_expired()
        if len(self._sessions) >= self._max_sessions:
            raise SandboxCapacityError("sandbox is at capacity")
        now = self._now()
        if charge_creation:
            cutoff = now - self._session_creation_window
            while self._session_creation_times and self._session_creation_times[0] < cutoff:
                self._session_creation_times.popleft()
            if len(self._session_creation_times) >= self._max_session_creations_per_window:
                raise SandboxSessionRateError("sandbox session creation rate reached")
            self._session_creation_times.append(now)
        session = SandboxSession(
            session_id=secrets.token_urlsafe(16),
            world=SandboxWorld(
                interpreter=self._new_interpreter(), started_at=default_start()
            ),
            created_at=now,
            last_activity_at=now,
            custom_message_limit=self._max_custom_messages_per_session,
        )
        self._sessions[session.session_id] = session
        self._schedule_expiry_locked(session)
        return session

    def get(self, session_id: str) -> SandboxSession | None:
        with self._guard:
            self._evict_expired()
            return self._sessions.get(session_id)

    def reset(self, session_id: str) -> SandboxSession:
        with self._guard:
            if session_id not in self._sessions:
                raise KeyError(session_id)
            self._drop_locked(session_id)
            return self._create_locked(charge_creation=False)

    def is_current(self, session_id: str, session: SandboxSession) -> bool:
        with self._guard:
            return self._sessions.get(session_id) is session

    def ensure_budget(self, session: SandboxSession) -> None:
        """Check the budget before doing work; charge only what succeeded."""
        if len(session.cards_played) >= MAX_CARDS_PER_SESSION:
            raise SandboxBudgetError("session card budget spent")

    def select_mode(
        self,
        session: SandboxSession,
        mode: SandboxMode,
        *,
        subject: str | None = None,
    ) -> None:
        if session.mode is mode:
            if mode is SandboxMode.FREE_PLAY and subject != session.thread_subject:
                raise SandboxModeError("free-play subject is fixed for this thread")
            return
        if session.mode is not SandboxMode.UNSELECTED:
            raise SandboxModeError("start over to switch sandbox modes")
        if mode is SandboxMode.FREE_PLAY and not subject:
            raise SandboxModeError("free-play mode requires a visible subject")
        session.mode = mode
        session.thread_subject = subject if mode is SandboxMode.FREE_PLAY else THREAD_SUBJECT
        self.touch(session)

    @staticmethod
    def require_mode(session: SandboxSession, mode: SandboxMode) -> None:
        if session.mode is not mode:
            raise SandboxModeError(
                f"choose {mode.value.replace('_', ' ')} mode before this action"
            )

    def touch(self, session: SandboxSession) -> None:
        with self._guard:
            if self._sessions.get(session.session_id) is not session:
                return
            session.last_activity_at = self._now()
            self._schedule_expiry_locked(session)

    def record_card(
        self,
        session: SandboxSession,
        card_id: str,
        note: str | None = None,
    ) -> None:
        session.cards_played.append(card_id)
        if note is not None:
            session.card_notes[card_id] = note
        # Preserve the exact click chronology inside the guided lane rather
        # than reconstructing it from authored Gmail timestamps.
        card = CARDS_BY_ID.get(card_id)
        if isinstance(card, MessageCard):
            session.thread_messages.append(
                SandboxThreadMessage(
                    message_id=card.card_id,
                    persona=card.persona,
                    sender="Jordan Ellis" if card.persona == "jordan" else "You",
                    subject=THREAD_SUBJECT,
                    body=card.body,
                    note=note or card.note,
                    custom=False,
                )
            )
        self.touch(session)

    def charge_custom_message(self, session: SandboxSession) -> str:
        """Atomically reserve one custom delivery and return its unique id."""
        with self._guard:
            if session.custom_messages_sent >= self._max_custom_messages_per_session:
                raise SandboxCustomMessageBudgetError(
                    "session custom-message budget spent"
                )
            self._charge_custom_rate_locked()
            session.custom_messages_sent += 1
            return f"sandbox-custom-{session.custom_messages_sent}"

    def charge_interpretation_retry(
        self,
        session: SandboxSession,
        message_id: str,
    ) -> int:
        """Reserve one bounded live retry without charging a new message."""

        with self._guard:
            attempts = session.interpretation_retry_counts.get(message_id, 0)
            if (
                message_id not in session.retryable_message_ids
                or attempts >= MAX_INTERPRETATION_RETRIES_PER_MESSAGE
                or session.interpretation_retries_sent
                >= MAX_INTERPRETATION_RETRIES_PER_SESSION
            ):
                raise SandboxInterpretationRetryError(
                    "interpretation retry is no longer available"
                )
            self._charge_custom_rate_locked()
            attempts += 1
            session.interpretation_retry_counts[message_id] = attempts
            session.interpretation_retries_sent += 1
            session.retryable_message_ids.discard(message_id)
            return attempts

    def _charge_custom_rate_locked(self) -> None:
        now = self._now()
        cutoff = now - self._custom_message_window
        while self._custom_message_times and self._custom_message_times[0] < cutoff:
            self._custom_message_times.popleft()
        if len(self._custom_message_times) >= self._max_custom_messages_per_window:
            raise SandboxCustomMessageRateError("custom-message rate limit reached")
        self._custom_message_times.append(now)

    def record_custom_message(
        self,
        session: SandboxSession,
        message_id: str,
        persona: str,
        body: str,
        note: str,
    ) -> None:
        subject = session.thread_subject
        if subject is None:
            raise SandboxModeError("free-play subject is missing")
        session.thread_messages.append(
            SandboxThreadMessage(
                message_id=message_id,
                persona=persona,
                sender="Jordan Ellis" if persona == "jordan" else "You",
                subject=subject,
                body=body,
                note=note,
                custom=True,
            )
        )
        self.touch(session)

    def record_interpretation_retry(
        self,
        session: SandboxSession,
        message_id: str,
        note: str,
    ) -> None:
        for index, message in enumerate(session.thread_messages):
            if message.message_id != message_id or not message.custom:
                continue
            session.thread_messages[index] = replace(message, note=note)
            self.touch(session)
            return
        raise SandboxInterpretationRetryError("custom message is no longer available")

    def active_count(self) -> int:
        with self._guard:
            self._evict_expired()
            return len(self._sessions)
