from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from commitmentos.api.dependencies.google_oidc import (
    GoogleDeliveryPrincipal,
    GoogleOidcDependency,
)
from commitmentos.application.commands.receive_gmail_signal import (
    MalformedSignalError,
    ReceiveGmailSignal,
    SignalRateLimitError,
    UnexpectedMailboxError,
)


class PubSubRouter:
    """Authenticated Gmail Pub/Sub push ingress.

    OIDC verification is a dependency so it resolves before any body work
    (§16.3). The route commits the coalesced sync request, creates the named
    source-sync task, and acknowledges with 204 — Pub/Sub's role ends here.
    """

    def __init__(
        self,
        receive_gmail_signal: ReceiveGmailSignal,
        pubsub_oidc: GoogleOidcDependency,
    ) -> None:
        self._receive_gmail_signal = receive_gmail_signal
        self._pubsub_oidc = pubsub_oidc

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/internal/pubsub", tags=["pubsub"])
        oidc = self._pubsub_oidc
        handler = self

        @router.post("/gmail", status_code=204)
        async def receive_gmail(
            request: Request,
            principal: GoogleDeliveryPrincipal = Depends(oidc),
        ) -> Response:
            try:
                envelope = await request.json()
            except Exception as error:  # noqa: BLE001 - malformed push body
                raise HTTPException(status_code=400, detail="malformed envelope") from error
            trace_id = getattr(request.state, "trace_id", "trace-pubsub")
            try:
                await handler._receive_gmail_signal.execute(envelope, trace_id)
            except MalformedSignalError as error:
                raise HTTPException(
                    status_code=400, detail="malformed gmail notification"
                ) from error
            except UnexpectedMailboxError as error:
                raise HTTPException(status_code=403, detail="unexpected mailbox") from error
            except SignalRateLimitError as error:
                raise HTTPException(status_code=429, detail="rate limited") from error
            return Response(status_code=204)

        return router
