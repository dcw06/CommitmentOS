from __future__ import annotations

from commitmentos.contracts.model_output import (
    CommitmentInterpretationV1,
    CommitmentSemanticProposalV1,
    DeadlineProposalV1,
    EvidenceSpanProposalV1,
    ModelOutputValidator,
)
from commitmentos.domain.commitments.identity import IdentityOperation
from commitmentos.domain.commitments.models import OwnershipType


def proposal(expression: str, value: str) -> CommitmentSemanticProposalV1:
    return CommitmentSemanticProposalV1(
        source_span_key="m1:span",
        normalized_outcome="Send the budget",
        description="",
        ownership_type=OwnershipType.MY_COMMITMENT,
        owner_display_name="Me",
        beneficiary_display_name="Reviewer",
        deadline=DeadlineProposalV1(expression, value, 0.95),
        proposed_effort_minutes=60,
        proposed_identity_operation=IdentityOperation.CREATE,
        target_commitment_id=None,
        evidence=(EvidenceSpanProposalV1("m1:span", "m1", "Send the budget"),),
        confidence=0.95,
    )


def normalized_deadline(expression: str, value: str) -> str:
    validation = ModelOutputValidator().validate(
        CommitmentInterpretationV1("extraction_v2", (proposal(expression, value),)),
        {"m1": "Please Send the budget on August 18."},
        frozenset(),
        deadline_timezone="America/Los_Angeles",
    )
    deadline = validation.proposals[0].proposal.deadline
    assert deadline is not None and deadline.proposed_value is not None
    return deadline.proposed_value


def test_date_only_deadline_defaults_to_configured_working_day_end() -> None:
    assert normalized_deadline(
        "on August 18", "2026-08-18T00:00:00-07:00"
    ) == "2026-08-18T17:30:00-07:00"


def test_explicit_clock_time_is_preserved() -> None:
    assert normalized_deadline(
        "on August 18 at 3 p.m.", "2026-08-18T15:00:00-07:00"
    ) == "2026-08-18T15:00:00-07:00"
    assert normalized_deadline(
        "Thursday at 4", "2026-08-20T16:00:00-07:00"
    ) == "2026-08-20T16:00:00-07:00"
