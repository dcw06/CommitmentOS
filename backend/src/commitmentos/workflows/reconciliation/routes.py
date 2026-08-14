from __future__ import annotations

from enum import StrEnum

from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class ReconciliationRoute(StrEnum):
    LOAD_OBSERVATION = "load_observation"
    LOAD_SOURCE_CONTEXT = "load_source_context"
    INTERPRET_COMMITMENT = "interpret_commitment"
    VALIDATE_INTERPRETATION = "validate_interpretation"
    RESOLVE_COMMITMENT_IDENTITY = "resolve_commitment_identity"
    UPSERT_EVIDENCE = "upsert_evidence"
    RECORD_EFFORT_INPUT_REQUIRED = "record_effort_input_required"
    VERIFY_COMPLETION = "verify_completion"
    LOAD_RECONCILIATION_STATE = "load_reconciliation_state"
    VALIDATE_PROJECTIONS = "validate_projections"
    CALCULATE_PORTFOLIO_FEASIBILITY = "calculate_portfolio_feasibility"
    PRODUCE_STABLE_PLAN = "produce_stable_plan"
    APPLY_POLICY = "apply_policy"
    RECORD_ACTION_APPROVAL_REQUIRED = "record_action_approval_required"
    WRITE_ACTION_OUTBOX = "write_action_outbox"
    FINALIZE_RECONCILIATION_RUN = "finalize_reconciliation_run"


class ReconciliationRouter:
    def after_observation(self, state: ReconciliationStateV1) -> ReconciliationRoute:
        ...

    def after_interpretation_validation(
        self,
        state: ReconciliationStateV1,
    ) -> ReconciliationRoute:
        ...

    def after_identity_resolution(self, state: ReconciliationStateV1) -> ReconciliationRoute:
        ...

    def after_evidence_upsert(self, state: ReconciliationStateV1) -> ReconciliationRoute:
        ...

    def after_policy(self, state: ReconciliationStateV1) -> ReconciliationRoute:
        ...

    def after_failure(self, state: ReconciliationStateV1) -> ReconciliationRoute:
        ...
