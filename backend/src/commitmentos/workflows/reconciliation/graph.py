from __future__ import annotations

from commitmentos.application.dto import ReconciliationOutcome, ReconciliationRequest
from commitmentos.workflows.reconciliation.phase1_workflow import (
    DurableReconciliationWorkflow,
)


class AdkReconciliationWorkflow:
    """Honest ADK boundary around the durable reconciliation controller.

    One named Cloud Task starts one bounded ADK run. The graph delegates
    exactly once to the transaction-aware controller and then emits a safe
    terminal summary. Planning, policy, and outbox remain explicit methods
    inside that controller; they are not represented by pass-through nodes.
    Calendar I/O remains outside the graph.
    """

    def __init__(
        self,
        inner: DurableReconciliationWorkflow,
        app_name: str = "commitmentos-reconciliation",
    ) -> None:
        self._inner = inner
        self._app_name = app_name

    async def execute(self, request: ReconciliationRequest) -> ReconciliationOutcome:
        from google.adk.runners import InMemoryRunner
        from google.adk.workflow import START, Workflow, node
        from google.genai import types as genai_types

        inner = self._inner
        outcome_holder: dict[str, ReconciliationOutcome] = {}

        # Node signatures stay unannotated: ADK derives edge schemas from the
        # annotations, and `parameter_binding="node_input"` matches parameters
        # by name against keys of the previous node's returned dict (the
        # Phase 0 §7 finding).
        @node(name="execute_durable_reconciliation", parameter_binding="node_input")
        async def execute_durable_reconciliation(node_input=None):  # noqa: ANN001, ANN202
            del node_input
            outcome = await inner.execute(request)
            outcome_holder["outcome"] = outcome
            return {
                "route_result": {
                    "status": outcome.status,
                    "durable_outcome_count": len(outcome.durable_outcome_ids),
                    "error_code": outcome.error_code,
                    "calendar_io_performed": False,
                }
            }

        @node(name="finalize_reconciliation_run", parameter_binding="node_input")
        async def finalize_reconciliation_run(route_result):  # noqa: ANN001, ANN202
            return {
                "run_id": request.run_id,
                "status": route_result["status"],
                "terminated": True,
            }

        workflow = Workflow(
            name="commitmentos_reconciliation",
            edges=[
                (
                    START,
                    execute_durable_reconciliation,
                    finalize_reconciliation_run,
                )
            ],
        )
        runner = InMemoryRunner(agent=workflow, app_name=self._app_name)
        session = await runner.session_service.create_session(
            app_name=self._app_name, user_id=request.user_id
        )
        async for _event in runner.run_async(
            user_id=request.user_id,
            session_id=session.id,
            new_message=genai_types.Content(
                role="user", parts=[genai_types.Part(text=request.run_id)]
            ),
        ):
            pass
        outcome = outcome_holder.get("outcome")
        if outcome is None:
            # The graph terminated without reaching the route stage.
            return ReconciliationOutcome(
                run_id=request.run_id,
                observation_id=request.observation_id,
                status="retryable_failure",
                durable_outcome_ids=(),
                error_code="adk_graph_incomplete",
                retryable=True,
                processing_fencing_token=request.processing_fence.fencing_token,
            )
        return outcome
