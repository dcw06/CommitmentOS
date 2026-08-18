"""Interpretation for the sandbox: live Gemini, cached, with a recorded floor.

A judge-facing surface must not be an unbounded model-spend endpoint, and it
must not go dark when a model call fails. Both are handled here rather than
by weakening the workflow:

* **Cached per canned message.** The card set is fixed, so the first session
  to send a card pays one live call and every later session reuses that
  result for the process's lifetime. Live output is still genuine model
  output on that exact input.
* **Recorded floor.** If no live interpreter is configured, or the call
  fails or is rejected by the strict wire schema, the card's recorded
  interpretation is used and the response says so, so a judge is never shown
  model output that did not happen.

The deterministic validator downstream is unchanged and applies to both
paths: evidence quotes must be exact substrings of the source message.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Mapping, Sequence

from commitmentos.application.ports.model_interpreter import (
    InterpretationResult,
    ModelInterpreter,
    ModelInvocationMetadata,
    ModelOutputParseError,
)
from commitmentos.contracts.model_output import (
    CommitmentInterpretationV1,
    parse_interpretation_wire,
    wire_to_contract,
)
from commitmentos.domain.commitments.identity import IdentityOperation
from commitmentos.sandbox.scenario import MESSAGES

logger = logging.getLogger(__name__)

RECORDED_MODEL_ID = "recorded-interpretation"


def _bind_candidate_target(
    interpretation: CommitmentInterpretationV1,
    candidate_commitments: Sequence[Mapping[str, str]],
) -> CommitmentInterpretationV1:
    """Fill an `update_existing` proposal's target from the candidate context.

    Commitment ids are content-derived and unknowable when the record is
    authored, so a recorded revision proposal carries none. The live model
    faces no such problem: the workflow passes it the candidate commitments
    with their ids, and it returns the one it means. Binding the recorded
    proposal to the sole candidate reproduces that, and leaves the
    deterministic validator's `identity_target_missing` rejection intact for
    every case it cannot be sure about.
    """
    if len(candidate_commitments) != 1:
        return interpretation
    target = str(candidate_commitments[0].get("commitment_id", ""))
    if not target:
        return interpretation
    proposals = tuple(
        replace(proposal, target_commitment_id=target)
        if proposal.proposed_identity_operation is IdentityOperation.UPDATE_EXISTING
        and not proposal.target_commitment_id
        else proposal
        for proposal in interpretation.proposals
    )
    return replace(interpretation, proposals=proposals)


def _recorded(card_body: str) -> CommitmentInterpretationV1 | None:
    for card in MESSAGES:
        if card.body == card_body:
            wire, errors = parse_interpretation_wire(card.recorded_wire)
            if wire is None:
                raise RuntimeError(f"recorded interpretation is invalid: {errors}")
            return wire_to_contract(wire)
    return None


class SandboxInterpreter(ModelInterpreter):
    """Prefers the live model, caches per card, falls back to the record."""

    def __init__(self, live: ModelInterpreter | None) -> None:
        self._live = live
        self._cache: dict[str, InterpretationResult] = {}
        self.last_source: str = "recorded"

    async def interpret_commitment(
        self,
        source_text: str,
        source_metadata: Mapping[str, str],
        candidate_commitments: Sequence[Mapping[str, str]],
    ) -> InterpretationResult:
        key = f"{source_text}|{len(candidate_commitments)}"
        cached = self._cache.get(key)
        if cached is not None:
            self.last_source = "live-cached"
            return cached
        if self._live is not None:
            try:
                result = await self._live.interpret_commitment(
                    source_text, source_metadata, candidate_commitments
                )
            except (ModelOutputParseError, Exception) as error:  # noqa: BLE001
                # A public demonstration degrades to the record rather than
                # failing; the response labels which path produced it.
                logger.warning(
                    "sandbox live interpretation unavailable, using record",
                    extra={"error_type": type(error).__name__},
                )
            else:
                self._cache[key] = result
                self.last_source = "live"
                return result

        interpretation = self._newest_recorded(source_text)
        if interpretation is None:
            raise ModelOutputParseError(("sandbox_no_recorded_interpretation",))
        interpretation = _bind_candidate_target(interpretation, candidate_commitments)
        self.last_source = "recorded"
        return InterpretationResult(
            interpretation=interpretation,
            metadata=ModelInvocationMetadata(
                model_id=RECORDED_MODEL_ID,
                prompt_version="commitment_interpretation_v2",
                schema_version="extraction_v2",
                thinking_level="low",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
            ),
        )

    @staticmethod
    def _newest_recorded(source_text: str) -> CommitmentInterpretationV1 | None:
        """The thread is rendered whole each time; interpret its last message.

        The workflow hands the model the entire thread, so the card being
        sent is the last one whose body appears in the rendered source.
        """
        newest: CommitmentInterpretationV1 | None = None
        position = -1
        for card in MESSAGES:
            index = source_text.find(card.body[:60])
            if index > position:
                candidate = _recorded(card.body)
                if candidate is not None:
                    newest = candidate
                    position = index
        return newest

    async def explain_decision(
        self,
        decision: Mapping[str, object],
        evidence: Sequence[Mapping[str, str]],
    ) -> tuple[str, ModelInvocationMetadata]:
        if self._live is not None:
            try:
                return await self._live.explain_decision(decision, evidence)
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "sandbox live explanation unavailable, using fallback",
                    extra={"error_type": type(error).__name__},
                )
        return (
            str(decision.get("fallback_explanation", "The plan was updated.")),
            ModelInvocationMetadata(
                model_id=RECORDED_MODEL_ID,
                prompt_version="explanation_v1",
                schema_version="explanation_v1",
                thinking_level="low",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
            ),
        )
