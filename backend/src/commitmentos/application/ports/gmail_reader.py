from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, Sequence


class SourceAuthorizationError(Exception):
    """The controlled credential can no longer be refreshed (grant revoked).

    Callers persist a visible `reauth_required` state and stop dependent work
    instead of retrying blindly (Phase 0 §8 finding: revocation is grant-wide
    and immediate)."""


class SourceCursorInvalidError(Exception):
    """The provider rejected the stored history cursor (Gmail 404 on
    startHistoryId); recovery requires a bounded resynchronization from a
    fresh baseline."""


@dataclass(frozen=True, slots=True)
class GmailHistoryChange:
    history_id: str
    message_ids: tuple[str, ...]
    label_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GmailHistoryPage:
    changes: tuple[GmailHistoryChange, ...]
    next_page_token: str | None
    latest_history_id: str


@dataclass(frozen=True, slots=True)
class GmailMailboxPage:
    """One bounded page used to rebuild a fresh history baseline."""

    message_ids: tuple[str, ...]
    next_page_token: str | None
    # Page one carries the history ID captured before the mailbox scan. Later
    # pages may omit it; the generation checkpoints the baseline durably.
    latest_history_id: str | None


@dataclass(frozen=True, slots=True)
class GmailMessage:
    message_id: str
    thread_id: str
    internal_date: datetime
    label_ids: tuple[str, ...]
    headers: Mapping[str, str]
    body_text: str
    payload_hash: str


@dataclass(frozen=True, slots=True)
class GmailWatch:
    history_id: str
    expiration: datetime


class GmailReader(Protocol):
    async def create_watch(self, user_id: str, topic_name: str, label_ids: Sequence[str]) -> GmailWatch:
        ...

    async def list_history(
        self,
        user_id: str,
        start_history_id: str,
        page_token: str | None,
    ) -> GmailHistoryPage:
        ...

    async def list_messages(
        self,
        user_id: str,
        page_token: str | None,
    ) -> GmailMailboxPage:
        """List one bounded Inbox-or-Sent page for cursor recovery.

        Page one carries a fresh mailbox history ID captured before scanning;
        callers publish it only after every page and normalized item commits.
        """
        ...

    async def get_message(self, user_id: str, message_id: str) -> GmailMessage | None:
        """Returns None when the message no longer exists (deleted or a
        discarded draft referenced by an old history record)."""
        ...

    async def get_thread(self, user_id: str, thread_id: str) -> Sequence[GmailMessage]:
        ...
