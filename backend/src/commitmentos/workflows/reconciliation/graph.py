from __future__ import annotations

from typing import Any

from commitmentos.application.dto import ReconciliationOutcome, ReconciliationRequest
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.workflows.reconciliation.phase1_workflow import (
    SeededReconciliationWorkflow,
)


class AdkReconciliationWorkflow:
    """ADK `Workflow` wrapper around the bounded reconciliation route.

    Fulfils the Phase 1 deviation note: with the Gemini interpretation node
    landed, production reconciliation runs through the deployed ADK graph
    mechanic proven in Phase 0 §7 — a typed, bounded `Workflow` executed by
    `InMemoryRunner` per named-task delivery, terminating without waiting on
    external I/O.

    Phase 3 gives portfolio work visible bounded stages in the graph:
    `calculate_portfolio_feasibility → produce_stable_plan → apply_policy`.
    The durable route remains the single transaction-aware authority used by
    both production and tests; these graph stages classify, execute, and
    surface its deterministic result without placing Calendar I/O in ADK.
    """

    def __init__(
        self,
        inner: SeededReconciliationWorkflow,
        unit_of_work: UnitOfWork,
        app_name: str = "commitmentos-reconciliation",
    ) -> None:
        self._inner = inner
        self._unit_of_work = unit_of_work
        self._app_name = app_name

    async def execute(self, request: ReconciliationRequest) -> ReconciliationOutcome:
        from google.adk.runners import InMemoryRunner
        from google.adk.workflow import START, Workflow, node
        from google.genai import types as genai_types

        inner = self._inner
        unit_of_work = self._unit_of_work
        outcome_holder: dict[str, ReconciliationOutcome] = {}

        # Node signatures stay unannotated: ADK derives edge schemas from the
        # annotations, and `parameter_binding="node_input"` matches parameters
        # by name against keys of the previous node's returned dict (the
        # Phase 0 §7 finding).
        @node(name="load_observation", parameter_binding="node_input")
        async def load_observation(node_input=None):  # noqa: ANN001, ANN202
            async def _load(repositories: RepositorySet) -> Any:
                return await repositories.observations.get(request.observation_id)

            observation = await unit_of_work.read(_load)
            return {
                "observation_ref": {
                    "observation_id": request.observation_id,
                    "observation_type": (
                        observation.observation_type.value if observation else "missing"
                    ),
                    "dispatch_generation": request.dispatch_generation,
                }
            }

        @node(name="calculate_portfolio_feasibility", parameter_binding="node_input")
        async def calculate_portfolio_feasibility(  # noqa: ANN001, ANN202
            observation_ref,
        ):
            return {
                "planning_context": {
                    **observation_ref,
                    "portfolio_scope": "all_active_commitments",
                    "single_allocation_required": True,
                }
            }

        @node(name="produce_stable_plan", parameter_binding="node_input")
        async def produce_stable_plan(planning_context):  # noqa: ANN001, ANN202
            outcome = await inner.execute(request)
            outcome_holder["outcome"] = outcome
            return {
                "plan_result": {
                    "planning_context": planning_context,
                    "status": outcome.status,
                    "durable_outcome_count": len(outcome.durable_outcome_ids),
                    "error_code": outcome.error_code,
                }
            }

        @node(name="apply_policy", parameter_binding="node_input")
        async def apply_policy(plan_result):  # noqa: ANN001, ANN202
            return {
                "route_result": {
                    "status": plan_result["status"],
                    "durable_outcome_count": plan_result["durable_outcome_count"],
                    "error_code": plan_result["error_code"],
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
                    load_observation,
                    calculate_portfolio_feasibility,
                    produce_stable_plan,
                    apply_policy,
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
