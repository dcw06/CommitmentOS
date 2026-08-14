from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from commitmentos.contracts.model_output import CommitmentInterpretationV1


class ModelOutputParseError(Exception):
    """The model returned output the strict wire schema rejects."""

    def __init__(self, error_codes: tuple[str, ...]) -> None:
        super().__init__(f"model output failed schema validation: {error_codes}")
        self.error_codes = error_codes


@dataclass(frozen=True, slots=True)
class ModelInvocationMetadata:
    model_id: str
    prompt_version: str
    schema_version: str
    thinking_level: str
    latency_ms: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class InterpretationResult:
    interpretation: CommitmentInterpretationV1
    metadata: ModelInvocationMetadata


class ModelInterpreter(Protocol):
    async def interpret_commitment(
        self,
        source_text: str,
        source_metadata: Mapping[str, str],
        candidate_commitments: Sequence[Mapping[str, str]],
    ) -> InterpretationResult:
        ...

    async def explain_decision(
        self,
        decision: Mapping[str, object],
        evidence: Sequence[Mapping[str, str]],
    ) -> tuple[str, ModelInvocationMetadata]:
        ...
