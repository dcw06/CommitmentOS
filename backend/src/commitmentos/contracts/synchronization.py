from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from commitmentos.contracts.tasks import SourceType
from commitmentos.domain.shared.types import CanonicalEncoder


class SyncGenerationMode(StrEnum):
    INCREMENTAL = "incremental"
    FULL_RESYNC = "full_resync"


class SyncGenerationStatus(StrEnum):
    STAGING = "staging"
    APPLYING = "applying"
    READY_TO_PUBLISH = "ready_to_publish"
    PUBLISHED = "published"
    FAILED = "failed"
    ABANDONED = "abandoned"


class SyncGenerationItemStatus(StrEnum):
    STAGED = "staged"
    APPLIED = "applied"


@dataclass(frozen=True, slots=True)
class SyncManifestHash:
    algorithm_version: str
    item_count: int
    digest: str


@dataclass(frozen=True, slots=True)
class SyncGeneration:
    sync_generation_id: str
    sync_request_id: str
    user_id: str
    source: SourceType
    mode: SyncGenerationMode
    status: SyncGenerationStatus
    base_published_cursor_revision: int
    provider_request_parameters: Mapping[str, str]
    current_page_sequence: int
    next_page_token: str | None
    candidate_next_cursor: str | None
    staged_manifest: SyncManifestHash
    applied_manifest: SyncManifestHash
    page_count: int
    staged_item_count: int
    applied_item_count: int
    outstanding_chunk_id: str | None
    full_sync_tombstones_complete: bool
    source_lease_key: str
    source_lease_owner: str
    source_fencing_token: str
    source_lease_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class SyncGenerationItem:
    sync_generation_item_id: str
    sync_generation_id: str
    page_sequence: int
    chunk_sequence: int
    source: SourceType
    external_id: str
    external_version: str
    payload_hash: str
    normalized_kind: str
    normalized_payload: Mapping[str, Any]
    status: SyncGenerationItemStatus
    staged_at: datetime
    applied_at: datetime | None


@dataclass(frozen=True, slots=True)
class SyncPageCheckpoint:
    sync_generation_id: str
    page_sequence: int
    staged_item_count: int
    committed_write_count: int
    estimated_commit_bytes: int
    page_manifest: SyncManifestHash
    aggregate_staged_manifest: SyncManifestHash
    next_page_token: str | None
    candidate_next_cursor: str | None
    final_provider_page: bool
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class SyncApplyCheckpoint:
    sync_generation_id: str
    chunk_sequence: int
    first_item_id: str
    last_item_id: str
    applied_item_count: int
    chunk_manifest: SyncManifestHash
    aggregate_applied_manifest: SyncManifestHash
    full_sync_tombstones_complete: bool
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class SyncPublicationBarrier:
    user_id: str
    source: SourceType
    sync_generation_id: str
    base_published_cursor_revision: int
    activated_at: datetime


@dataclass(frozen=True, slots=True)
class SyncCursor:
    user_id: str
    source: SourceType
    revision: int
    published_cursor: str | None
    published_generation_id: str | None
    publish_in_progress_generation_id: str | None
    generation_counter: int
    calendar_state_revision: int
    full_resync_required: bool
    updated_at: datetime


MANIFEST_ALGORITHM_V1 = "sync-manifest-xor-v1"
_DIGEST_BYTES = 32


class SyncManifestHasher:
    """Order- and chunk-boundary-independent manifest over generation items.

    Staging aggregates page by page while apply aggregates by item-budget
    chunks, so the two aggregates only match for identical item sets if the
    fold is commutative and associative: each item hashes independently and
    digests combine by XOR, with item counts summing. This is a consistency
    check between two internally produced sets, not an adversarial integrity
    boundary.
    """

    @staticmethod
    def _item_digest(item: SyncGenerationItem) -> bytes:
        return hashlib.sha256(
            CanonicalEncoder.encode(
                [
                    "sync-manifest-item:v1",
                    item.sync_generation_item_id,
                    item.external_id,
                    item.external_version,
                    item.payload_hash,
                ]
            )
        ).digest()

    def empty(self, algorithm_version: str = MANIFEST_ALGORITHM_V1) -> SyncManifestHash:
        return SyncManifestHash(
            algorithm_version=algorithm_version,
            item_count=0,
            digest=(b"\x00" * _DIGEST_BYTES).hex(),
        )

    def for_items(
        self,
        items: tuple[SyncGenerationItem, ...],
        algorithm_version: str = MANIFEST_ALGORITHM_V1,
    ) -> SyncManifestHash:
        accumulator = bytearray(_DIGEST_BYTES)
        for item in items:
            digest = self._item_digest(item)
            for index in range(_DIGEST_BYTES):
                accumulator[index] ^= digest[index]
        return SyncManifestHash(
            algorithm_version=algorithm_version,
            item_count=len(items),
            digest=bytes(accumulator).hex(),
        )

    def combine(
        self,
        current: SyncManifestHash,
        addition: SyncManifestHash,
    ) -> SyncManifestHash:
        if current.algorithm_version != addition.algorithm_version:
            raise ValueError("cannot combine manifests across algorithm versions")
        left = bytes.fromhex(current.digest)
        right = bytes.fromhex(addition.digest)
        combined = bytes(a ^ b for a, b in zip(left, right, strict=True))
        return SyncManifestHash(
            algorithm_version=current.algorithm_version,
            item_count=current.item_count + addition.item_count,
            digest=combined.hex(),
        )

    def matches(self, left: SyncManifestHash, right: SyncManifestHash) -> bool:
        return (
            left.algorithm_version == right.algorithm_version
            and left.item_count == right.item_count
            and left.digest == right.digest
        )


class SyncIdFactory:
    """Deterministic synchronization identities (architecture §11.2)."""

    @staticmethod
    def generation_id(
        source: SourceType,
        user_id: str,
        base_cursor_revision: int,
        generation_number: int,
    ) -> str:
        return CanonicalEncoder.hash(
            [
                "sync-generation:v1",
                source.value,
                user_id,
                base_cursor_revision,
                generation_number,
            ]
        )

    @staticmethod
    def item_id(sync_generation_id: str, external_id: str, external_version: str) -> str:
        return CanonicalEncoder.hash(
            ["sync-generation-item:v1", sync_generation_id, external_id, external_version]
        )
