from __future__ import annotations

from typing import Any, Sequence

from google.cloud.firestore_v1.base_query import FieldFilter


class FirestoreCleanupDocumentStore:
    """Raw-client document store backing the audited cleanup command.

    Only the developer cleanup uses this surface; production commands go
    through the transactional unit of work. Every operation is bounded.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def _query(self, collection: str, filters: Sequence[tuple[str, str, Any]]) -> Any:
        query = self._client.collection(collection)
        for field, op, value in filters:
            query = query.where(filter=FieldFilter(field, op, value))
        return query

    async def list_ids_where(
        self,
        collection: str,
        filters: Sequence[tuple[str, str, Any]],
        limit: int,
    ) -> Sequence[str]:
        return [
            snapshot.id
            async for snapshot in self._query(collection, filters).limit(limit).stream()
        ]

    async def count_where(
        self,
        collection: str,
        filters: Sequence[tuple[str, str, Any]],
    ) -> int:
        result = await self._query(collection, filters).count().get()
        return int(result[0][0].value)

    async def purge_where(
        self,
        collection: str,
        filters: Sequence[tuple[str, str, Any]],
        limit: int,
    ) -> int:
        removed = 0
        async for snapshot in self._query(collection, filters).limit(limit).stream():
            await snapshot.reference.delete()
            removed += 1
        return removed
