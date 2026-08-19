"""Interpretation for the sandbox: live Gemini, bounded cache, recorded card floor.

A judge-facing surface must not be an unbounded model-spend endpoint, and it
must not go dark when a model call fails. Both are handled here rather than
by weakening the workflow:

* **Guided-card cache only.** The fixed authored contexts can share a bounded
  process cache. Judge-authored text and its structured result stay inside its
  session and are never inserted into that shared cache.
* **Recorded floor.** If no live interpreter is configured, or the call
  fails or is rejected by the strict wire schema, a guided card's recorded
  interpretation is used and the response says so, so a judge is never shown
  model output that did not happen.

Free-play text never uses the recorded floor. If live interpretation is not
available, it is rejected visibly rather than being misrepresented as a
canned card result.

The deterministic validator downstream is unchanged and applies to both
paths: evidence quotes must be exact substrings of the source message.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Awaitable, Callable, Mapping, Sequence, TypeVar

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
from commitmentos.domain.commitments.models import OwnershipType
from commitmentos.sandbox.scenario import MESSAGES, PACIFIC, MessageCard

logger = logging.getLogger(__name__)

RECORDED_MODEL_ID = "recorded-interpretation"
DETERMINISTIC_EXPLANATION_MODEL_ID = "deterministic-plan-diff"
MAX_CACHE_ENTRIES = 256
MAX_CACHE_LOCKS = 512
MAX_MODEL_CALLS_PER_WINDOW = 12
MODEL_CALL_WINDOW_SECONDS = 60.0
MAX_CONCURRENT_MODEL_CALLS = 2
MAX_MODEL_CALLS_PER_SESSION = 12
# A cached non-live result is a degradation, not a fact about the model. It
# is kept only briefly so a transient failure or a one-off nonconforming
# output cannot pin "recorded-fallback" for the process lifetime — later
# sessions get a fresh live attempt, still bounded by the rolling gate.
FALLBACK_CACHE_TTL_SECONDS = 600.0

T = TypeVar("T")


class SandboxModelBudgetError(RuntimeError):
    """A public model-call budget is exhausted."""


class SandboxModelCallGate:
    """One process-wide rolling and concurrency gate for every model method."""

    def __init__(
        self,
        *,
        max_calls_per_window: int = MAX_MODEL_CALLS_PER_WINDOW,
        window_seconds: float = MODEL_CALL_WINDOW_SECONDS,
        max_concurrent_calls: int = MAX_CONCURRENT_MODEL_CALLS,
    ) -> None:
        self._max_calls_per_window = max_calls_per_window
        self._window_seconds = window_seconds
        self._call_times: OrderedDict[int, float] = OrderedDict()
        self._sequence = 0
        self._guard = threading.Lock()
        self._concurrency = asyncio.Semaphore(max_concurrent_calls)

    async def invoke(self, call: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        with self._guard:
            cutoff = now - self._window_seconds
            while self._call_times:
                first_key, first_time = next(iter(self._call_times.items()))
                if first_time >= cutoff:
                    break
                self._call_times.pop(first_key)
            if len(self._call_times) >= self._max_calls_per_window:
                raise SandboxModelBudgetError("sandbox model rate limit reached")
            self._sequence += 1
            self._call_times[self._sequence] = now
        async with self._concurrency:
            return await call()


class BoundedSandboxModelInterpreter(ModelInterpreter):
    """Per-session model allowance layered over the process-wide gate."""

    def __init__(
        self,
        inner: ModelInterpreter,
        gate: SandboxModelCallGate,
        *,
        max_calls: int = MAX_MODEL_CALLS_PER_SESSION,
    ) -> None:
        self._inner = inner
        self._gate = gate
        self._max_calls = max_calls
        self._calls = 0

    async def _invoke(self, call: Callable[[], Awaitable[T]]) -> T:
        if self._calls >= self._max_calls:
            raise SandboxModelBudgetError("sandbox session model budget spent")
        self._calls += 1
        return await self._gate.invoke(call)

    async def interpret_commitment(
        self,
        source_text: str,
        source_metadata: Mapping[str, str],
        candidate_commitments: Sequence[Mapping[str, str]],
    ) -> InterpretationResult:
        return await self._invoke(
            lambda: self._inner.interpret_commitment(
                source_text, source_metadata, candidate_commitments
            )
        )

    async def explain_decision(
        self,
        decision: Mapping[str, object],
        evidence: Sequence[Mapping[str, str]],
    ) -> tuple[str, ModelInvocationMetadata]:
        return await self._invoke(lambda: self._inner.explain_decision(decision, evidence))


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    result: InterpretationResult
    origin: str
    stored_at: float


class SandboxInterpretationCache:
    """Process-local result cache shared by isolated per-session interpreters.

    The values are safe to share because the key includes the complete candidate
    context, not only its length. Per-key async locks also ensure two judges do
    not pay for the same first live invocation concurrently. Live results stay
    for the cache's lifetime (the process); non-live results expire after
    `FALLBACK_CACHE_TTL_SECONDS` so a failure is retried rather than treated as
    permanently authoritative.
    """

    def __init__(
        self,
        max_entries: int = MAX_CACHE_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._max_entries = max_entries
        self._clock = clock
        self._guard = threading.Lock()

    def get(self, key: str) -> _CacheEntry | None:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if (
                entry.origin != "live"
                and self._clock() - entry.stored_at > FALLBACK_CACHE_TTL_SECONDS
            ):
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key: str, result: InterpretationResult, origin: str) -> None:
        with self._guard:
            self._entries[key] = _CacheEntry(result, origin, self._clock())
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def lock_for(self, key: str) -> asyncio.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._locks.move_to_end(key)
            # Keep active/waited-on locks. Old unlocked keys are safe to drop;
            # the corresponding result has already been cached or the call is
            # no longer in flight.
            while len(self._locks) > MAX_CACHE_LOCKS:
                old_key, old_lock = next(iter(self._locks.items()))
                if old_lock.locked():
                    self._locks.move_to_end(old_key)
                    if all(candidate.locked() for candidate in self._locks.values()):
                        break
                    continue
                self._locks.pop(old_key)
            return lock

    def size(self) -> int:
        with self._guard:
            return len(self._entries)


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


def _newest_card(source_text: str) -> MessageCard | None:
    newest: MessageCard | None = None
    position = -1
    for card in MESSAGES:
        index = source_text.find(card.body[:60])
        if index > position:
            newest = card
            position = index
    return newest


def _cache_key(
    source_text: str,
    source_metadata: Mapping[str, str],
    candidate_commitments: Sequence[Mapping[str, str]],
) -> str:
    context = json.dumps(
        {
            "source": source_text,
            "metadata": dict(source_metadata),
            "candidates": [dict(candidate) for candidate in candidate_commitments],
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


def _deadline_day(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(PACIFIC).date()
    except ValueError:
        return None


def _proposal_errors(
    card: MessageCard,
    proposal,  # noqa: ANN001 - CommitmentProposalV1, kept untyped like the wire helpers
    expected_proposal,  # noqa: ANN001
    candidate_ids: set[str],
) -> tuple[str, ...]:
    """The per-proposal semantic checks for one fixed judge card."""

    errors: list[str] = []
    outcome = proposal.normalized_outcome.casefold()
    if not all(token in outcome for token in ("vendor", "comparison", "deck")):
        errors.append("outcome")
    if proposal.ownership_type is not expected_proposal.ownership_type:
        errors.append("ownership")
    if _deadline_day(proposal.deadline.proposed_value if proposal.deadline else None) != (
        _deadline_day(
            expected_proposal.deadline.proposed_value
            if expected_proposal.deadline
            else None
        )
    ):
        errors.append("deadline_day")

    if card.card_id == "msg_request":
        if proposal.proposed_identity_operation is not IdentityOperation.CREATE:
            errors.append("request_identity")
    elif card.card_id == "msg_accept":
        if proposal.proposed_identity_operation not in {
            IdentityOperation.CREATE,
            IdentityOperation.UPDATE_EXISTING,
        }:
            errors.append("acceptance_identity")
        if (
            proposal.proposed_identity_operation is IdentityOperation.UPDATE_EXISTING
            and proposal.target_commitment_id not in candidate_ids
        ):
            errors.append("acceptance_target")
    elif card.card_id == "msg_deadline_change":
        if proposal.ownership_type is not OwnershipType.MY_COMMITMENT:
            errors.append("deadline_ownership")
        if proposal.proposed_identity_operation is not IdentityOperation.UPDATE_EXISTING:
            errors.append("deadline_identity")
        if proposal.target_commitment_id not in candidate_ids:
            errors.append("deadline_target")
    return tuple(errors)


def _select_conforming_proposal(
    card: MessageCard,
    interpretation: CommitmentInterpretationV1,
    candidate_commitments: Sequence[Mapping[str, str]],
) -> tuple[CommitmentInterpretationV1 | None, tuple[str, ...]]:
    """Narrow live output to the authored branch instead of rejecting wholesale.

    The production validator still checks evidence and authority. This extra
    sandbox-only boundary prevents a schema-valid model variation from turning
    the authored one-commitment story into a different (but safely escalated)
    product branch while the explanatory copy claims the authored branch ran.

    The model sometimes returns extra proposals for the full-thread render
    (measured: a restatement of the existing commitment beside the authored
    revision). Selection is a narrowing, never a weakening: the accepted
    proposal must pass every per-proposal check, and the downstream
    deterministic validator is unchanged. Only when no proposal conforms does
    the card fall back to its recorded interpretation.
    """

    if not interpretation.proposals:
        return None, ("proposal_count",)
    expected = _recorded(card.body)
    if expected is None or len(expected.proposals) != 1:
        return None, ("recorded_contract_missing",)
    expected_proposal = expected.proposals[0]
    candidate_ids = {
        str(candidate.get("commitment_id", ""))
        for candidate in candidate_commitments
        if candidate.get("commitment_id")
    }
    all_errors: list[str] = []
    for proposal in interpretation.proposals:
        errors = _proposal_errors(card, proposal, expected_proposal, candidate_ids)
        if not errors:
            if len(interpretation.proposals) > 1:
                logger.info(
                    "sandbox live interpretation narrowed to the conforming "
                    "proposal (%d returned)",
                    len(interpretation.proposals),
                )
            return replace(interpretation, proposals=(proposal,)), ()
        all_errors.extend(errors)
    return None, tuple(dict.fromkeys(all_errors))


class SandboxInterpreter(ModelInterpreter):
    """Prefers the live model, caches per card, falls back to the record."""

    def __init__(
        self,
        live: ModelInterpreter | None,
        cache: SandboxInterpretationCache | None = None,
    ) -> None:
        self._live = live
        self._cache = cache or SandboxInterpretationCache()
        self.last_source: str = "not-run"
        self._expected_card_id: str | None = None
        self._custom_message = False

    def begin_guided_message(self, card_id: str) -> None:
        """Pin the card being delivered; source text cannot spoof this choice."""
        self._expected_card_id = card_id
        self._custom_message = False

    def begin_custom_message(self) -> None:
        """Mark one delivery as free-play, where canned fallback is forbidden."""
        self._expected_card_id = None
        self._custom_message = True

    def end_message(self) -> None:
        self._expected_card_id = None
        self._custom_message = False

    async def interpret_commitment(
        self,
        source_text: str,
        source_metadata: Mapping[str, str],
        candidate_commitments: Sequence[Mapping[str, str]],
    ) -> InterpretationResult:
        # Custom source text and structured output are session-private. Check
        # this before even looking at the shared guided-card cache so copying a
        # card body can never select a cached authored answer.
        if self._custom_message:
            return await self._interpret_custom(
                source_text, source_metadata, candidate_commitments
            )

        key = _cache_key(source_text, source_metadata, candidate_commitments)
        cached = self._cache.get(key)
        if cached is not None:
            self.last_source = f"{cached.origin}-cached"
            return cached.result

        async with self._cache.lock_for(key):
            cached = self._cache.get(key)
            if cached is not None:
                self.last_source = f"{cached.origin}-cached"
                return cached.result

            fallback_origin = "recorded"
            card = next(
                (
                    candidate
                    for candidate in MESSAGES
                    if candidate.card_id == self._expected_card_id
                ),
                None,
            ) or _newest_card(source_text)
            if self._live is not None:
                fallback_origin = "recorded-fallback"
                try:
                    result = await self._live.interpret_commitment(
                        source_text, source_metadata, candidate_commitments
                    )
                except Exception as error:  # noqa: BLE001
                    # A public demonstration degrades to the record rather than
                    # failing; the response labels which path produced it.
                    logger.warning(
                        "sandbox live interpretation unavailable, using record",
                        extra={"error_type": type(error).__name__},
                    )
                else:
                    bound = _bind_candidate_target(
                        result.interpretation, candidate_commitments
                    )
                    if card is not None:
                        narrowed, semantic_errors = _select_conforming_proposal(
                            card, bound, candidate_commitments
                        )
                    else:
                        narrowed, semantic_errors = None, ("unknown_card",)
                    if narrowed is not None:
                        result = replace(result, interpretation=narrowed)
                        self._cache.put(key, result, "live")
                        self.last_source = "live"
                        return result
                    # The codes ride in the message so structured logging that
                    # drops unknown extras still records why the card fell back.
                    logger.warning(
                        "sandbox live interpretation violated card contract "
                        "(%s), using record",
                        ",".join(semantic_errors),
                    )

            interpretation = _recorded(card.body) if card is not None else None
            if interpretation is None:
                raise ModelOutputParseError(("sandbox_no_recorded_interpretation",))
            interpretation = _bind_candidate_target(
                interpretation, candidate_commitments
            )
            result = InterpretationResult(
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
            self._cache.put(key, result, fallback_origin)
            self.last_source = fallback_origin
            return result

    async def _interpret_custom(
        self,
        source_text: str,
        source_metadata: Mapping[str, str],
        candidate_commitments: Sequence[Mapping[str, str]],
    ) -> InterpretationResult:
        """Run arbitrary text live, without ever borrowing a canned answer."""
        if self._live is None:
            self.last_source = "custom-unavailable"
            raise ModelOutputParseError(("sandbox_custom_interpretation_unavailable",))
        try:
            result = await self._live.interpret_commitment(
                source_text, source_metadata, candidate_commitments
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "sandbox custom interpretation unavailable",
                extra={"error_type": type(error).__name__},
            )
            self.last_source = "custom-unavailable"
            raise ModelOutputParseError(
                ("sandbox_custom_interpretation_unavailable",)
            ) from error
        self.last_source = "live-custom"
        return result

    async def explain_decision(
        self,
        decision: Mapping[str, object],
        evidence: Sequence[Mapping[str, str]],
    ) -> tuple[str, ModelInvocationMetadata]:
        # Deliberately deterministic — never a live call. The workflow awaits
        # this inside the repair path, so a model round-trip here would put up
        # to the 20 s transport timeout between a conflict and its repaired
        # response (measured live: 4.8-19.7 s on the conflict card versus
        # ~0.9 s for every other action). The explanation is non-authoritative
        # narration; the plan-diff fallback the workflow supplies is honest
        # and instant. Production keeps live explanations.
        del evidence
        return (
            str(decision.get("fallback_explanation", "The plan was updated.")),
            ModelInvocationMetadata(
                model_id=DETERMINISTIC_EXPLANATION_MODEL_ID,
                prompt_version="explanation_v1",
                schema_version="explanation_v1",
                thinking_level="low",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
            ),
        )
