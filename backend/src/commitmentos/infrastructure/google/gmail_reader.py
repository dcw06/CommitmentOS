from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import datetime, timezone
from typing import Any, Sequence

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build as build_google_api
from googleapiclient.errors import HttpError

from commitmentos.application.ports.gmail_reader import (
    GmailHistoryChange,
    GmailHistoryPage,
    GmailMailboxPage,
    GmailMessage,
    GmailWatch,
    SourceAuthorizationError,
    SourceCursorInvalidError,
)
from commitmentos.domain.shared.types import CanonicalEncoder
from commitmentos.infrastructure.google.credentials import ControlledCredentialsProvider

HISTORY_PAGE_SIZE = 25
# The filter enum is singular ("messageAdded") while the response field is
# plural ("messagesAdded") — the discovery document rejects the plural form.
_RELEVANT_HISTORY_TYPES = ("messageAdded",)


def message_payload_hash(
    message_id: str,
    internal_date: datetime,
    subject: str,
    body_text: str,
) -> str:
    """Content hash of an immutable Gmail message (labels excluded).

    Gmail message content never changes after delivery; label-only changes
    must not mint a new producer version, so the hash covers content fields
    only.
    """
    body_digest = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    return CanonicalEncoder.hash(
        ["gmail-message:v1", message_id, internal_date, subject, body_digest]
    )


class GoogleGmailReader:
    """Bounded read-only Gmail access for the controlled account."""

    def __init__(
        self,
        credentials_provider: ControlledCredentialsProvider,
        history_page_size: int = HISTORY_PAGE_SIZE,
    ) -> None:
        self._credentials_provider = credentials_provider
        self._history_page_size = history_page_size

    def _service(self) -> Any:
        return build_google_api(
            "gmail",
            "v1",
            credentials=self._credentials_provider.credentials(),
            cache_discovery=False,
        )

    async def create_watch(
        self,
        user_id: str,
        topic_name: str,
        label_ids: Sequence[str],
    ) -> GmailWatch:
        def _blocking() -> GmailWatch:
            response = (
                self._service()
                .users()
                .watch(
                    userId="me",
                    body={
                        "topicName": topic_name,
                        "labelIds": list(label_ids),
                        "labelFilterBehavior": "INCLUDE",
                    },
                )
                .execute()
            )
            expiration_ms = int(response["expiration"])
            return GmailWatch(
                history_id=str(response["historyId"]),
                expiration=datetime.fromtimestamp(expiration_ms / 1000, tz=timezone.utc),
            )

        return await asyncio.to_thread(_blocking)

    async def list_messages(
        self,
        user_id: str,
        page_token: str | None,
    ) -> GmailMailboxPage:
        def _blocking() -> GmailMailboxPage:
            baseline_history_id = None
            request_kwargs: dict[str, Any] = {
                "userId": "me",
                "maxResults": self._history_page_size,
                # Gmail's brace syntax is OR. Downstream normalization still
                # verifies labels on the fetched message before staging.
                "q": "{in:inbox in:sent}",
            }
            if page_token is not None:
                request_kwargs["pageToken"] = page_token
            try:
                if page_token is None:
                    # Capture the incremental baseline before scanning. Mail
                    # arriving during later pages is therefore replayed by
                    # history.list after this generation publishes.
                    profile = self._service().users().getProfile(userId="me").execute()
                    baseline_history_id = str(profile["historyId"])
                response = self._service().users().messages().list(**request_kwargs).execute()
                next_token = response.get("nextPageToken")
            except RefreshError as error:
                self._credentials_provider.invalidate()
                raise SourceAuthorizationError("gmail credential refresh failed") from error
            return GmailMailboxPage(
                message_ids=tuple(
                    str(message["id"])
                    for message in response.get("messages", ())
                    if message.get("id")
                ),
                next_page_token=next_token,
                latest_history_id=baseline_history_id,
            )

        return await asyncio.to_thread(_blocking)

    async def list_history(
        self,
        user_id: str,
        start_history_id: str,
        page_token: str | None,
    ) -> GmailHistoryPage:
        def _blocking() -> GmailHistoryPage:
            request_kwargs: dict[str, Any] = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "maxResults": self._history_page_size,
                "historyTypes": list(_RELEVANT_HISTORY_TYPES),
            }
            if page_token is not None:
                request_kwargs["pageToken"] = page_token
            try:
                response = self._service().users().history().list(**request_kwargs).execute()
            except RefreshError as error:
                self._credentials_provider.invalidate()
                raise SourceAuthorizationError("gmail credential refresh failed") from error
            except HttpError as error:
                status = error.resp.status if error.resp is not None else 0
                if status == 404:
                    # Gmail signals an expired/invalid startHistoryId with 404.
                    raise SourceCursorInvalidError(
                        f"history cursor {start_history_id} is no longer valid"
                    ) from error
                raise
            changes: list[GmailHistoryChange] = []
            for record in response.get("history", []):
                message_ids: list[str] = []
                label_ids: set[str] = set()
                for added in record.get("messagesAdded", []):
                    message = added.get("message", {})
                    if message.get("id"):
                        message_ids.append(message["id"])
                        label_ids.update(message.get("labelIds", []))
                if message_ids:
                    changes.append(
                        GmailHistoryChange(
                            history_id=str(record.get("id", "")),
                            message_ids=tuple(message_ids),
                            label_ids=tuple(sorted(label_ids)),
                        )
                    )
            return GmailHistoryPage(
                changes=tuple(changes),
                next_page_token=response.get("nextPageToken"),
                latest_history_id=str(response.get("historyId", start_history_id)),
            )

        return await asyncio.to_thread(_blocking)

    async def get_message(self, user_id: str, message_id: str) -> GmailMessage | None:
        def _blocking() -> GmailMessage | None:
            try:
                payload = (
                    self._service()
                    .users()
                    .messages()
                    .get(userId="me", id=message_id, format="full")
                    .execute()
                )
            except RefreshError as error:
                self._credentials_provider.invalidate()
                raise SourceAuthorizationError("gmail credential refresh failed") from error
            except HttpError as error:
                status = error.resp.status if error.resp is not None else 0
                if status in (404, 410):
                    # History can reference messages that no longer exist
                    # (discarded drafts, deletions); the mailbox moved on.
                    return None
                raise
            return self._parse_message(payload)

        return await asyncio.to_thread(_blocking)

    async def get_thread(self, user_id: str, thread_id: str) -> Sequence[GmailMessage]:
        def _blocking() -> list[GmailMessage]:
            response = (
                self._service()
                .users()
                .threads()
                .get(userId="me", id=thread_id, format="full")
                .execute()
            )
            messages = [self._parse_message(m) for m in response.get("messages", [])]
            messages.sort(key=lambda m: (m.internal_date, m.message_id))
            return messages

        return await asyncio.to_thread(_blocking)

    def _parse_message(self, payload: Any) -> GmailMessage:
        headers = {
            header["name"].lower(): header.get("value", "")
            for header in payload.get("payload", {}).get("headers", [])
        }
        internal_date = datetime.fromtimestamp(
            int(payload.get("internalDate", "0")) / 1000, tz=timezone.utc
        )
        body_text = self._extract_text_body(payload.get("payload", {}))
        message_id = payload["id"]
        return GmailMessage(
            message_id=message_id,
            thread_id=payload.get("threadId", ""),
            internal_date=internal_date,
            label_ids=tuple(sorted(payload.get("labelIds", []))),
            headers=headers,
            body_text=body_text,
            payload_hash=message_payload_hash(
                message_id, internal_date, headers.get("subject", ""), body_text
            ),
        )

    def _extract_text_body(self, payload: Any) -> str:
        if payload.get("mimeType", "").startswith("text/plain"):
            data = payload.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
        for part in payload.get("parts", []) or []:
            text = self._extract_text_body(part)
            if text:
                return text
        return ""
