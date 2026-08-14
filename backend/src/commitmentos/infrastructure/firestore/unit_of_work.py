from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from google.cloud.firestore_v1.base_query import FieldFilter

from commitmentos.application.dto import FencedLease
from commitmentos.application.ports.unit_of_work import RepositorySet
from commitmentos.infrastructure.firestore.client import FirestoreTransactionRunner
from commitmentos.infrastructure.firestore.serializers import SerializerRegistry

T = TypeVar("T")


class StagedWriteError(RuntimeError):
    ...


class FirestoreTransactionContext:
    """Read-through/write-behind document access for one transaction attempt.

    Firestore requires every read to precede every write inside a transaction,
    but application commands interleave read-check-write repository calls. The
    context therefore performs reads immediately (through the transaction for
    serializable isolation) and stages writes in an overlay that `flush()`
    applies to the transaction once the command completes. Reads consult the
    overlay first so a command observes its own staged writes. Queries do not
    consult the overlay; commands must run queries before staging writes that
    would affect them.
    """

    def __init__(self, client: Any, transaction: Any) -> None:
        self._client = client
        self._transaction = transaction
        self._staged: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._created: set[tuple[str, str]] = set()
        self._order: list[tuple[str, str]] = []

    @property
    def is_transactional(self) -> bool:
        return self._transaction is not None

    def _reference(self, collection: str, document_id: str) -> Any:
        return self._client.collection(collection).document(document_id)

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        key = (collection, document_id)
        if key in self._staged:
            staged = self._staged[key]
            return dict(staged) if staged is not None else None
        reference = self._reference(collection, document_id)
        if self._transaction is not None:
            snapshot = await reference.get(transaction=self._transaction)
        else:
            snapshot = await reference.get()
        return snapshot.to_dict() if snapshot.exists else None

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]],
        order_by: tuple[str, str] | None = None,
        limit: int | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        query: Any = self._client.collection(collection)
        for field, op, value in filters:
            query = query.where(filter=FieldFilter(field, op, value))
        if order_by is not None:
            field, direction = order_by
            query = query.order_by(field, direction=direction)
        if limit is not None:
            query = query.limit(limit)
        results: list[tuple[str, dict[str, Any]]] = []
        stream = query.stream(transaction=self._transaction) if self._transaction is not None else query.stream()
        async for snapshot in stream:
            results.append((snapshot.id, snapshot.to_dict()))
        return results

    def _stage(self, collection: str, document_id: str, value: dict[str, Any] | None) -> None:
        key = (collection, document_id)
        if key not in self._staged:
            self._order.append(key)
        self._staged[key] = value

    def stage_set(self, collection: str, document_id: str, value: dict[str, Any]) -> None:
        self._stage(collection, document_id, dict(value))

    def stage_create(self, collection: str, document_id: str, value: dict[str, Any]) -> None:
        # Callers must have verified absence via get(); Firestore's create also
        # enforces it at commit time as a backstop.
        self._stage(collection, document_id, dict(value))
        self._created.add((collection, document_id))

    def stage_delete(self, collection: str, document_id: str) -> None:
        self._stage(collection, document_id, None)

    def has_staged_writes(self) -> bool:
        return bool(self._order)

    def flush(self) -> None:
        if not self._order:
            return
        if self._transaction is None:
            raise StagedWriteError("write operations require a transactional unit of work")
        for key in self._order:
            collection, document_id = key
            reference = self._reference(collection, document_id)
            value = self._staged[key]
            if value is None:
                self._transaction.delete(reference)
            elif key in self._created:
                self._transaction.create(reference, value)
            else:
                self._transaction.set(reference, value)
        self._order.clear()
        self._staged.clear()
        self._created.clear()


class FirestoreUnitOfWork:
    def __init__(self, transaction_runner: FirestoreTransactionRunner, serializers: SerializerRegistry) -> None:
        self._runner = transaction_runner
        self._serializers = serializers

    async def run(self, operation: Callable[[RepositorySet], Awaitable[T]]) -> T:
        async def _transactional(transaction: Any) -> T:
            context = FirestoreTransactionContext(self._runner.client, transaction)
            repositories = self._repositories(context)
            result = await operation(repositories)
            context.flush()
            return result

        return await self._runner.run(_transactional)

    async def read(self, operation: Callable[[RepositorySet], Awaitable[T]]) -> T:
        context = FirestoreTransactionContext(self._runner.client, None)
        repositories = self._repositories(context)
        result = await operation(repositories)
        if context.has_staged_writes():
            raise StagedWriteError("read-only unit of work cannot stage writes")
        return result

    async def run_fenced(
        self,
        fence: FencedLease,
        operation: Callable[[RepositorySet], Awaitable[T]],
    ) -> T:
        async def _fenced(repositories: RepositorySet) -> T:
            await repositories.processing_leases.verify(fence, datetime.now(timezone.utc))
            return await operation(repositories)

        return await self.run(_fenced)

    def _repositories(self, context: FirestoreTransactionContext) -> Any:
        from commitmentos.infrastructure.firestore.repositories.implementations import (
            FirestoreRepositorySet,
        )

        return FirestoreRepositorySet(context, self._serializers)
