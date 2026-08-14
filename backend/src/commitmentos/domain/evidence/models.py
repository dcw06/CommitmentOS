from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SourceReference:
    source: str
    external_id: str
    external_version: str
    thread_id: str | None


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    span_key: str
    excerpt: str
    start_offset: int | None
    end_offset: int | None


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    commitment_id: str
    source_reference: SourceReference
    span: EvidenceSpan
    confidence: float
    model_version: str
    schema_version: str
    created_at: datetime


class EvidenceFactory:
    def create(
        self,
        commitment_id: str,
        observation_id: str,
        source_reference: SourceReference,
        span: EvidenceSpan,
        confidence: float,
        model_version: str,
        schema_version: str,
        created_at: datetime,
    ) -> Evidence:
        ...
