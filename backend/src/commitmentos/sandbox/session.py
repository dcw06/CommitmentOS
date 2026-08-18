"""Per-judge sandbox sessions: isolated, capped, and self-expiring.

Each browser gets its own `SandboxWorld` keyed by an opaque session id. The
store is deliberately process-local: sandbox state is a demonstration
artifact, never durable truth, so a Cloud Run instance recycle simply drops
it and the judge starts a fresh story. Caps bound what a public surface can
cost — a maximum number of concurrent worlds, an idle expiry, and a per
session card budget.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from commitmentos.application.ports.model_interpreter import ModelInterpreter
from commitmentos.sandbox.interpreter import SandboxInterpreter
from commitmentos.sandbox.world import SandboxWorld, default_start

MAX_SESSIONS = 40
IDLE_EXPIRY = timedelta(minutes=45)
MAX_CARDS_PER_SESSION = 60


class SandboxCapacityError(RuntimeError):
    """The sandbox is at its concurrent-session ceiling."""


class SandboxBudgetError(RuntimeError):
    """This session has spent its card budget."""


@dataclass
class SandboxSession:
    session_id: str
    world: SandboxWorld
    created_at: datetime
    last_used_at: datetime
    cards_played: list[str] = field(default_factory=list)


class SandboxSessionStore:
    def __init__(
        self,
        live_interpreter: ModelInterpreter | None,
        *,
        max_sessions: int = MAX_SESSIONS,
        idle_expiry: timedelta = IDLE_EXPIRY,
    ) -> None:
        self._sessions: dict[str, SandboxSession] = {}
        # One interpreter across sessions: the card set is fixed, so its cache
        # is what keeps a public surface from re-paying for the same message.
        self._interpreter = SandboxInterpreter(live_interpreter)
        self._max_sessions = max_sessions
        self._idle_expiry = idle_expiry

    @property
    def interpreter(self) -> SandboxInterpreter:
        return self._interpreter

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _evict_expired(self) -> None:
        cutoff = self._now() - self._idle_expiry
        for session_id in [
            key for key, value in self._sessions.items() if value.last_used_at < cutoff
        ]:
            self._sessions.pop(session_id, None)

    def create(self) -> SandboxSession:
        self._evict_expired()
        if len(self._sessions) >= self._max_sessions:
            raise SandboxCapacityError("sandbox is at capacity")
        now = self._now()
        session = SandboxSession(
            session_id=secrets.token_urlsafe(16),
            world=SandboxWorld(interpreter=self._interpreter, started_at=default_start()),
            created_at=now,
            last_used_at=now,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> SandboxSession | None:
        self._evict_expired()
        session = self._sessions.get(session_id)
        if session is not None:
            session.last_used_at = self._now()
        return session

    def reset(self, session_id: str) -> SandboxSession:
        self._sessions.pop(session_id, None)
        return self.create()

    def ensure_budget(self, session: SandboxSession) -> None:
        """Check the budget before doing work; charge only what succeeded."""
        if len(session.cards_played) >= MAX_CARDS_PER_SESSION:
            raise SandboxBudgetError("session card budget spent")

    def record_card(self, session: SandboxSession, card_id: str) -> None:
        session.cards_played.append(card_id)

    def active_count(self) -> int:
        self._evict_expired()
        return len(self._sessions)
