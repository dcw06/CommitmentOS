from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, time
from enum import StrEnum
from typing import Literal, Mapping
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from commitmentos.domain.commitments.identity import IdentityOperation
from commitmentos.domain.commitments.models import OwnershipType

EXTRACTION_SCHEMA_V2 = "extraction_v2"

DEADLINE_CONFIDENCE_FLOOR = 0.6
OWNERSHIP_CONFIDENCE_FLOOR = 0.6


class ModelValidationDisposition(StrEnum):
    ACCEPTED = "accepted"
    CONFIRMATION_REQUIRED = "confirmation_required"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class DeadlineProposalV1:
    source_expression: str
    proposed_value: str | None
    confidence: float


@dataclass(frozen=True, slots=True)
class EvidenceSpanProposalV1:
    span_key: str
    message_id: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class CommitmentSemanticProposalV1:
    source_span_key: str
    normalized_outcome: str
    description: str
    ownership_type: OwnershipType
    owner_display_name: str | None
    beneficiary_display_name: str | None
    deadline: DeadlineProposalV1 | None
    proposed_effort_minutes: int | None
    proposed_identity_operation: IdentityOperation
    target_commitment_id: str | None
    evidence: tuple[EvidenceSpanProposalV1, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class CommitmentInterpretationV1:
    schema_version: str
    proposals: tuple[CommitmentSemanticProposalV1, ...]


@dataclass(frozen=True, slots=True)
class ProposalValidationResult:
    proposal: CommitmentSemanticProposalV1
    disposition: ModelValidationDisposition
    error_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelValidationResult:
    disposition: ModelValidationDisposition
    proposals: tuple[ProposalValidationResult, ...]
    error_codes: tuple[str, ...]


# ----------------------------------------------------------------------
# Wire schema: the strict pydantic contract for everything Gemini returns.
# A sanitized copy guides the API (`response_schema` rejects $ref and
# additionalProperties); this model remains the authoritative validator.
# ----------------------------------------------------------------------


class EvidenceSpanWireV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)
    quote: str = Field(min_length=5, max_length=300)


class DeadlineWireV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_expression: str = Field(min_length=1, max_length=200)
    proposed_value: AwareDatetime | None = None
    confidence: float = Field(ge=0, le=1)


class CommitmentProposalWireV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ownership_type: Literal[
        "my_commitment", "request_to_me", "commitment_to_me", "ambiguous"
    ]
    normalized_outcome: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=500)
    beneficiary_display_name: str | None = Field(default=None, max_length=100)
    deadline: DeadlineWireV2 | None = None
    proposed_effort_minutes: int | None = Field(default=None, ge=15, le=2400)
    identity_operation: Literal[
        "create",
        "update_existing",
        "supersede",
        "cancel_existing",
        "ignore",
        "ambiguous",
    ]
    target_commitment_id: str | None = None
    evidence: list[EvidenceSpanWireV2] = Field(min_length=1, max_length=6)
    confidence: float = Field(ge=0, le=1)


class InterpretationWireV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["extraction_v2"]
    proposals: list[CommitmentProposalWireV2] = Field(max_length=5)


_WHITESPACE = re.compile(r"\s+")
_EXPLICIT_CLOCK_TIME = re.compile(
    r"\b(?:noon|midnight|morning|afternoon|evening|\d{1,2}:\d{2})\b|"
    r"\b\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)(?=\W|$)|"
    r"\b(?:at|around|before|after)\s+(?:[01]?\d|2[0-3])\b|"
    r"\bby\s+(?:[1-9]|1[0-2])\b",
    re.IGNORECASE,
)

_RESERVED_DELIMITER_TAGS = (
    "untrusted_source_messages",
    "message",
    "candidate_commitments",
    "candidate",
)


def neutralize_untrusted_delimiters(text: str) -> str:
    """Untrusted text must not be able to fabricate or close the prompt's
    data delimiters (§13.2). Angle brackets on the reserved tag names are
    rewritten so an embedded `</untrusted_source_messages>` stays data."""
    for tag in _RESERVED_DELIMITER_TAGS:
        text = text.replace(f"<{tag}", f"[{tag}").replace(f"</{tag}", f"[/{tag}")
    return text


def collapse_text(text: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text)).strip().lower()


def derive_span_key(message_id: str, primary_quote: str) -> str:
    """Stable source-span identity from the message and its anchoring quote.

    Reproducible whenever the model quotes the same span again, which is what
    the dismissed-span rule requires: a dismissed span must not resurface
    unchanged (plan §9.6). Multiple distinct commitments in one message anchor
    to different quotes and therefore receive distinct keys.
    """
    digest = hashlib.sha256(collapse_text(primary_quote).encode("utf-8")).hexdigest()
    return f"{message_id}:span-{digest[:16]}"


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slug(text: str | None) -> str:
    return _SLUG_STRIP.sub("-", collapse_text(text or "")).strip("-") or "none"


def derive_semantic_fingerprint(
    ownership_type: OwnershipType,
    normalized_outcome: str,
    beneficiary_display_name: str | None,
) -> str:
    return (
        f"{ownership_type.value}:{_slug(normalized_outcome)}:"
        f"{_slug(beneficiary_display_name)}"
    )


def parse_interpretation_wire(
    raw_json: str,
) -> tuple[InterpretationWireV2 | None, tuple[str, ...]]:
    try:
        return InterpretationWireV2.model_validate_json(raw_json), ()
    except ValidationError as error:
        codes = tuple(
            f"schema:{item['type']}:{'.'.join(str(p) for p in item['loc'])}"
            for item in error.errors()
        )
        return None, codes


def wire_to_contract(wire: InterpretationWireV2) -> CommitmentInterpretationV1:
    proposals = []
    for proposal in wire.proposals:
        primary = proposal.evidence[0]
        span_key = derive_span_key(primary.message_id, primary.quote)
        proposals.append(
            CommitmentSemanticProposalV1(
                source_span_key=span_key,
                normalized_outcome=proposal.normalized_outcome,
                description=proposal.description,
                ownership_type=OwnershipType(proposal.ownership_type),
                owner_display_name=None,
                beneficiary_display_name=proposal.beneficiary_display_name,
                deadline=(
                    DeadlineProposalV1(
                        source_expression=proposal.deadline.source_expression,
                        proposed_value=(
                            proposal.deadline.proposed_value.isoformat()
                            if proposal.deadline.proposed_value is not None
                            else None
                        ),
                        confidence=proposal.deadline.confidence,
                    )
                    if proposal.deadline is not None
                    else None
                ),
                proposed_effort_minutes=proposal.proposed_effort_minutes,
                proposed_identity_operation=IdentityOperation(proposal.identity_operation),
                target_commitment_id=proposal.target_commitment_id,
                evidence=tuple(
                    EvidenceSpanProposalV1(
                        span_key=derive_span_key(span.message_id, span.quote),
                        message_id=span.message_id,
                        excerpt=span.quote,
                    )
                    for span in proposal.evidence
                ),
                confidence=proposal.confidence,
            )
        )
    return CommitmentInterpretationV1(
        schema_version=wire.schema_version,
        proposals=tuple(proposals),
    )


def gemini_response_schema() -> dict:
    """The sanitized guidance copy for the Gemini API.

    The response_schema proto rejects `additionalProperties` and `$ref`
    (Phase 0 §7 finding), so definitions are inlined and that key dropped;
    `InterpretationWireV2` stays authoritative for everything returned.
    """
    schema = InterpretationWireV2.model_json_schema()
    definitions = schema.pop("$defs", {})

    def walk(node: object) -> object:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(definitions[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "additionalProperties"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)  # type: ignore[return-value]


class ModelOutputValidator:
    """Deterministic validation of parsed model output (plan §13.2).

    Confidence never grants authority: uncertain ownership, deadline, or
    identity routes to confirmation, and structural violations reject the
    proposal outright. Evidence quotes must be exact substrings of the
    delimited untrusted source, which is the anti-injection anchor — output
    that cannot cite its source is discarded.
    """

    def validate(
        self,
        interpretation: CommitmentInterpretationV1,
        message_bodies: Mapping[str, str],
        allowed_target_commitment_ids: frozenset[str],
        deadline_timezone: str = "UTC",
        working_day_end: time = time(17, 30),
    ) -> ModelValidationResult:
        if interpretation.schema_version != EXTRACTION_SCHEMA_V2:
            return ModelValidationResult(
                disposition=ModelValidationDisposition.REJECTED,
                proposals=(),
                error_codes=("unknown_schema_version",),
            )
        # The model reads delimiter-neutralized text, so quotes are matched
        # against the same transformation of the original bodies.
        collapsed_bodies = {
            message_id: collapse_text(neutralize_untrusted_delimiters(body))
            for message_id, body in message_bodies.items()
        }
        results: list[ProposalValidationResult] = []
        for proposal in interpretation.proposals:
            proposal = self._normalize_date_only_deadline(
                proposal,
                deadline_timezone,
                working_day_end,
            )
            results.append(
                self._validate_proposal(proposal, collapsed_bodies, allowed_target_commitment_ids)
            )
        if any(r.disposition == ModelValidationDisposition.REJECTED for r in results):
            overall = ModelValidationDisposition.REJECTED
        elif any(
            r.disposition == ModelValidationDisposition.CONFIRMATION_REQUIRED for r in results
        ):
            overall = ModelValidationDisposition.CONFIRMATION_REQUIRED
        else:
            overall = ModelValidationDisposition.ACCEPTED
        return ModelValidationResult(
            disposition=overall,
            proposals=tuple(results),
            error_codes=tuple(
                code for result in results for code in result.error_codes
            ),
        )

    @staticmethod
    def _normalize_date_only_deadline(
        proposal: CommitmentSemanticProposalV1,
        timezone_name: str,
        working_day_end: time,
    ) -> CommitmentSemanticProposalV1:
        deadline = proposal.deadline
        if (
            deadline is None
            or deadline.proposed_value is None
            or _EXPLICIT_CLOCK_TIME.search(deadline.source_expression)
        ):
            return proposal
        try:
            parsed = datetime.fromisoformat(
                deadline.proposed_value.replace("Z", "+00:00")
            )
        except ValueError:
            # The strict wire/schema parser owns malformed-value rejection;
            # normalization must never turn bad model output into a crash.
            return proposal
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return proposal
        zone = ZoneInfo(timezone_name)
        normalized = datetime.combine(
            parsed.astimezone(zone).date(),
            working_day_end,
            tzinfo=zone,
        )
        return replace(
            proposal,
            deadline=replace(deadline, proposed_value=normalized.isoformat()),
        )

    def _validate_proposal(
        self,
        proposal: CommitmentSemanticProposalV1,
        collapsed_bodies: Mapping[str, str],
        allowed_target_commitment_ids: frozenset[str],
    ) -> ProposalValidationResult:
        errors: list[str] = []
        needs_confirmation = False

        if not proposal.evidence:
            errors.append("commitment_without_evidence")
        for span in proposal.evidence:
            body = collapsed_bodies.get(span.message_id)
            if body is None:
                errors.append(f"evidence_unknown_message:{span.message_id}")
            elif collapse_text(span.excerpt) not in body:
                errors.append(f"evidence_quote_not_in_source:{span.message_id}")

        deadline = proposal.deadline
        if deadline is not None:
            if deadline.proposed_value is not None and not deadline.source_expression.strip():
                errors.append("deadline_value_without_expression")
            if deadline.proposed_value is None and deadline.confidence > 0:
                needs_confirmation = True
            if deadline.confidence < DEADLINE_CONFIDENCE_FLOOR:
                needs_confirmation = True

        if proposal.ownership_type == OwnershipType.AMBIGUOUS:
            needs_confirmation = True
        if proposal.confidence < OWNERSHIP_CONFIDENCE_FLOOR:
            needs_confirmation = True

        operation = proposal.proposed_identity_operation
        if operation in (
            IdentityOperation.UPDATE_EXISTING,
            IdentityOperation.SUPERSEDE,
            IdentityOperation.CANCEL_EXISTING,
        ):
            if proposal.target_commitment_id is None:
                errors.append("identity_target_missing")
            elif proposal.target_commitment_id not in allowed_target_commitment_ids:
                errors.append("identity_target_not_in_candidate_set")
        elif operation == IdentityOperation.CREATE and proposal.target_commitment_id:
            errors.append("create_with_target")
        elif operation == IdentityOperation.AMBIGUOUS:
            needs_confirmation = True

        if errors:
            disposition = ModelValidationDisposition.REJECTED
        elif needs_confirmation:
            disposition = ModelValidationDisposition.CONFIRMATION_REQUIRED
        else:
            disposition = ModelValidationDisposition.ACCEPTED
        return ProposalValidationResult(
            proposal=proposal,
            disposition=disposition,
            error_codes=tuple(errors),
        )
