from __future__ import annotations

from typing import Any, Awaitable, Callable, TypeVar

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore

T = TypeVar("T")


class FirestoreClientFactory:
    def __init__(self, project_id: str, database_id: str) -> None:
        self._project_id = project_id
        self._database_id = database_id

    def create(self) -> Any:
        return firestore.AsyncClient(project=self._project_id, database=self._database_id)


class FirestoreTransactionRunner:
    def __init__(self, client: Any, maximum_attempts: int) -> None:
        self._client = client
        self._maximum_attempts = maximum_attempts

    @property
    def client(self) -> Any:
        return self._client

    async def run(self, operation: Callable[[Any], Awaitable[T]]) -> T:
        transaction = self._client.transaction(max_attempts=self._maximum_attempts)

        wrapped = firestore.async_transactional(operation)
        return await wrapped(transaction)

    async def read(self, operation: Callable[[Any], Awaitable[T]]) -> T:
        return await operation(None)

    def classify_error(self, error: Exception) -> str:
        if isinstance(error, google_exceptions.Aborted):
            return "transaction_contention"
        if isinstance(error, google_exceptions.AlreadyExists):
            return "already_exists"
        if isinstance(error, google_exceptions.FailedPrecondition):
            return "failed_precondition"
        if isinstance(
            error,
            (
                google_exceptions.ServiceUnavailable,
                google_exceptions.DeadlineExceeded,
                google_exceptions.InternalServerError,
            ),
        ):
            return "retryable_infrastructure"
        return "unclassified"
