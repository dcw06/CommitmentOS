"""Route trust-contract tests for the Phase 1 API surface.

Every rejected request must cause zero durable side effects: no approval
resolution, no observation, no task dispatch (checklist D1 session-mutation
row and the §13.5 endpoint contracts).
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import CONTROLLED_USER, Phase1App
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.api.dependencies.google_oidc import GoogleOidcDependency
from commitmentos.api.middleware.request_context import RequestContextMiddleware
from commitmentos.api.routers.approvals import ApprovalsRouter
from commitmentos.api.routers.commitments import CommitmentsRouter
from commitmentos.api.routers.controls import ControlsRouter
from commitmentos.api.routers.dashboard import DashboardRouter
from commitmentos.api.routers.plans import PlansRouter
from commitmentos.api.routers.task_handlers import TaskHandlersRouter
from commitmentos.api.routers.work_blocks import WorkBlocksRouter
from commitmentos.application.ports.identity_verifier import VerifiedIdentity
from commitmentos.application.queries.get_commitment import GetCommitment
from commitmentos.application.queries.get_system_status import GetSystemStatus
from commitmentos.application.queries.get_today import GetToday
from commitmentos.application.queries.list_activity import ListActivity
from commitmentos.application.queries.list_commitments import ListCommitments
from commitmentos.domain.commitments.models import (
    Commitment,
    Deadline,
    Effort,
    LifecycleStatus,
    OwnershipType,
)
from commitmentos.domain.planning.models import PlannerRunStatus
from commitmentos.domain.progress.models import (
    UserEditState,
    WorkBlock,
    WorkBlockExecutionState,
)

SESSION_TOKEN = "session-token-0001"
CSRF_SECRET = "csrf-secret-0001"
TASKS_SA = "commitmentos-tasks@test.iam.gserviceaccount.com"
TASKS_AUDIENCE = "https://service.test/internal/tasks"


class FakeIdentityVerifier:
    """Accepts tokens of the form 'valid:<email>' for a fixed audience."""

    async def verify_oidc_token(self, token, expected_audience, allowed_subjects):
        if not token.startswith("valid:"):
            raise ValueError("invalid token")
        email = token.split(":", 1)[1]
        if email not in allowed_subjects:
            raise ValueError("unexpected identity")
        return VerifiedIdentity(
            subject="sub", email=email, audience=expected_audience, issuer="https://accounts.google.com"
        )

    async def verify_google_user_token(self, *args, **kwargs):
        raise NotImplementedError


@pytest.fixture
def api(app: Phase1App) -> TestClient:
    session_hash = hashlib.sha256(SESSION_TOKEN.encode()).hexdigest()
    now = app.clock.now()
    app.store.setdefault("web_sessions", {})[session_hash] = {
        "user_id": CONTROLLED_USER,
        "email": "controlled@example.invalid",
        "csrf_secret": CSRF_SECRET,
        "created_at": now,
        "expires_at": now + timedelta(hours=12),
        "revoked_at": None,
    }
    session = ControlledSessionDependency(app.uow, app.clock, CONTROLLED_USER)
    csrf = CsrfProtection()
    tasks_oidc = GoogleOidcDependency(
        FakeIdentityVerifier(), TASKS_AUDIENCE, TASKS_SA, "tasks"
    )
    fastapi_app = FastAPI()
    fastapi_app.add_middleware(RequestContextMiddleware)
    fastapi_app.include_router(
        ApprovalsRouter(app.resolve_approval, session, csrf).build()
    )
    fastapi_app.include_router(
        ControlsRouter(app.change_control, session, csrf).build()
    )
    fastapi_app.include_router(
        WorkBlocksRouter(app.record_work_check_in, session, csrf).build()
    )
    fastapi_app.include_router(
        PlansRouter(app.request_plan_undo, session, csrf).build()
    )
    fastapi_app.include_router(
        TaskHandlersRouter(
            app.reconcile, app.executor, app.synchronize_source, tasks_oidc
        ).build()
    )
    fastapi_app.include_router(
        CommitmentsRouter(
            GetCommitment(app.uow), ListCommitments(app.uow), session
        ).build()
    )
    status_query = GetSystemStatus(app.uow, app.clock)
    fastapi_app.include_router(
        DashboardRouter(
            GetToday(app.uow, app.clock, status_query),
            ListActivity(app.uow),
            status_query,
            session,
            "America/Los_Angeles",
        ).build()
    )
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    client.app_state = app  # type: ignore[attr-defined]
    return client


async def _pending_approval(app: Phase1App):
    await app.seed_golden_observation()
    await app.run_reconciliation_tasks()

    async def _load(repositories):
        return list(await repositories.approvals.list_pending(CONTROLLED_USER))

    return (await app.uow.read(_load))[0]


async def _check_in_fixture(app: Phase1App) -> WorkBlock:
    now = app.clock.now()
    commitment = Commitment(
        commitment_id="commitment-route-check-in",
        user_id=CONTROLLED_USER,
        revision=1,
        source_thread_id="thread-route-check-in",
        semantic_fingerprint="my_commitment:route-check-in:none",
        title="Route check-in fixture",
        description="",
        ownership_type=OwnershipType.MY_COMMITMENT,
        owner={"type": "user"},
        beneficiary={"display_name": "Reviewer"},
        deadline=Deadline(
            value=now + timedelta(days=2),
            timezone="UTC",
            confidence=1.0,
            evidence_id="source-evidence",
            source_expression="in two days",
            rule_version="test",
        ),
        effort=Effort(60, 1.0, 60, now),
        lifecycle_status=LifecycleStatus.ACTIVE,
        completion_evidence_id=None,
        completed_at=None,
        plan_revision=1,
        projection=None,
        policy_profile="default_personal",
        created_at=now,
        updated_at=now,
    )
    block = WorkBlock(
        work_block_id="block-route-check-in",
        commitment_id=commitment.commitment_id,
        revision=1,
        calendar_id="primary",
        calendar_event_id="event-route-check-in",
        calendar_snapshot_id=None,
        duration_minutes=60,
        execution_state=WorkBlockExecutionState.AWAITING_CHECK_IN,
        scheduled_start=now - timedelta(hours=1),
        scheduled_end=now,
        verified_minutes=0,
        completion_evidence_id=None,
        user_edit_state=UserEditState.NONE,
        plan_revision=1,
    )

    async def _save(repositories) -> None:
        await repositories.commitments.save(commitment, None)
        await repositories.work_blocks.save(block, None)

    await app.uow.run(_save)
    return block


async def _planner_run_fixture(app: Phase1App) -> str:
    await _check_in_fixture(app)
    plan = await app.portfolio_planning.calculate(CONTROLLED_USER)

    async def _save(repositories) -> None:
        await repositories.planner_runs.create(
            replace(
                plan,
                status=PlannerRunStatus.PUBLISHED,
                published_at=app.clock.now(),
            )
        )

    await app.uow.run(_save)
    return plan.planner_run_id


class TestControlledMutationContracts:
    async def test_missing_session_is_rejected_with_zero_side_effects(
        self, api: TestClient, app: Phase1App
    ) -> None:
        approval = await _pending_approval(app)
        before = copy.deepcopy(app.store)
        tasks_before = len(app.task_dispatcher.reconciliation_tasks)
        response = api.post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            json={"expected_revision": 1, "decision": "approve", "confirmed_minutes": 180},
        )
        assert response.status_code == 401
        assert app.store == before
        assert len(app.task_dispatcher.reconciliation_tasks) == tasks_before

    async def test_dashboard_failure_reads_require_session_and_are_read_only(
        self, api: TestClient, app: Phase1App
    ) -> None:
        before = copy.deepcopy(app.store)
        path = "/api/v1/dashboard/system-status"
        assert api.get(path).status_code == 401
        response = api.get(
            path,
            cookies={"commitmentos_session": SESSION_TOKEN},
        )
        assert response.status_code == 200
        assert "failure_states" in response.json()
        today = api.get(
            "/api/v1/dashboard/today",
            cookies={"commitmentos_session": SESSION_TOKEN},
        )
        assert today.status_code == 200
        assert "visible_failure_states" in today.json()
        assert app.store == before

    async def test_missing_csrf_token_is_rejected_with_zero_side_effects(
        self, api: TestClient, app: Phase1App
    ) -> None:
        approval = await _pending_approval(app)
        before = copy.deepcopy(app.store)
        response = api.post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            json={"expected_revision": 1, "decision": "approve", "confirmed_minutes": 180},
            cookies={"commitmentos_session": SESSION_TOKEN},
        )
        assert response.status_code == 403
        assert app.store == before

    async def test_invalid_csrf_token_is_rejected(self, api: TestClient, app: Phase1App) -> None:
        approval = await _pending_approval(app)
        before = copy.deepcopy(app.store)
        response = api.post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            json={"expected_revision": 1, "decision": "approve", "confirmed_minutes": 180},
            cookies={"commitmentos_session": SESSION_TOKEN},
            headers={"X-CSRF-Token": "wrong-token"},
        )
        assert response.status_code == 403
        assert app.store == before

    async def test_valid_session_and_csrf_resolves_approval(
        self, api: TestClient, app: Phase1App
    ) -> None:
        approval = await _pending_approval(app)
        response = api.post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            json={"expected_revision": 1, "decision": "approve", "confirmed_minutes": 180},
            cookies={"commitmentos_session": SESSION_TOKEN},
            headers={"X-CSRF-Token": CSRF_SECRET},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        stored = app.store["approvals"][approval["approval_id"]]
        assert stored["status"] == "resolved"

    async def test_calendar_decision_choice_reaches_the_domain_command(
        self, api: TestClient, app: Phase1App
    ) -> None:
        block = await _check_in_fixture(app)
        approval_id = "calendar-decision-route"
        app.store.setdefault("approvals", {})[approval_id] = {
            "approval_id": approval_id,
            "user_id": CONTROLLED_USER,
            "commitment_id": block.commitment_id,
            "commitment_revision": 1,
            "request_type": "calendar_invalid_move_decision",
            "payload": {
                "work_block_id": block.work_block_id,
                "expected_work_block_revision": block.revision,
                "options": [
                    "restore_approved_slot",
                    "reschedule_safely",
                    "pause_commitment",
                ],
            },
            "continuation_type": "calendar_invalid_move_decision",
            "policy_reason": "explicit_calendar_edit_requires_choice",
            "status": "pending",
            "revision": 1,
            "created_at": app.clock.now(),
            "expires_at": app.clock.now() + timedelta(days=7),
        }

        response = api.post(
            f"/api/v1/approvals/{approval_id}/resolve",
            json={
                "expected_revision": 1,
                "decision": "approve",
                "choice": "restore_approved_slot",
            },
            cookies={"commitmentos_session": SESSION_TOKEN},
            headers={"X-CSRF-Token": CSRF_SECRET},
        )

        assert response.status_code == 200
        stored = app.store["approvals"][approval_id]
        assert stored["decision"]["payload"]["choice"] == "restore_approved_slot"

    async def test_expired_session_is_rejected(self, api: TestClient, app: Phase1App) -> None:
        approval = await _pending_approval(app)
        session_hash = hashlib.sha256(SESSION_TOKEN.encode()).hexdigest()
        app.store["web_sessions"][session_hash]["expires_at"] = app.clock.now() - timedelta(
            minutes=1
        )
        response = api.post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            json={"expected_revision": 1, "decision": "approve", "confirmed_minutes": 180},
            cookies={"commitmentos_session": SESSION_TOKEN},
            headers={"X-CSRF-Token": CSRF_SECRET},
        )
        assert response.status_code == 401

    async def test_revoked_session_is_rejected(self, api: TestClient, app: Phase1App) -> None:
        approval = await _pending_approval(app)
        session_hash = hashlib.sha256(SESSION_TOKEN.encode()).hexdigest()
        app.store["web_sessions"][session_hash]["revoked_at"] = app.clock.now()
        response = api.post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            json={"expected_revision": 1, "decision": "approve", "confirmed_minutes": 180},
            cookies={"commitmentos_session": SESSION_TOKEN},
            headers={"X-CSRF-Token": CSRF_SECRET},
        )
        assert response.status_code == 401

    async def test_control_change_requires_csrf(self, api: TestClient, app: Phase1App) -> None:
        before = copy.deepcopy(app.store)
        response = api.post(
            "/api/v1/controls/change",
            json={
                "control_name": "automatic_actions",
                "target_mode": "paused",
                "reason": "test",
                "expected_control_epoch": 1,
            },
            cookies={"commitmentos_session": SESSION_TOKEN},
        )
        assert response.status_code == 403
        assert app.store == before

    async def test_work_check_in_requires_session_and_csrf_before_body_validation(
        self, api: TestClient, app: Phase1App
    ) -> None:
        await _check_in_fixture(app)
        before = copy.deepcopy(app.store)
        path = "/api/v1/work-blocks/block-route-check-in/check-in"
        assert api.post(path, json={"invalid": "body"}).status_code == 401
        assert app.store == before
        assert api.post(
            path,
            json={"invalid": "body"},
            cookies={"commitmentos_session": SESSION_TOKEN},
        ).status_code == 403
        assert app.store == before

    async def test_valid_guarded_work_check_in_records_progress(
        self, api: TestClient, app: Phase1App
    ) -> None:
        block = await _check_in_fixture(app)
        response = api.post(
            f"/api/v1/work-blocks/{block.work_block_id}/check-in",
            json={
                "expected_revision": 1,
                "idempotency_key": "route-check-in-1",
                "completed": True,
                "verified_minutes": 45,
                "checked_in_at": app.clock.now().isoformat(),
            },
            cookies={"commitmentos_session": SESSION_TOKEN},
            headers={"X-CSRF-Token": CSRF_SECRET},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        stored = app.store["work_blocks"][block.work_block_id]
        assert stored["verified_minutes"] == 45
        assert stored["execution_state"] == "completed"

    async def test_plan_undo_is_guarded_and_only_creates_reconciliation_input(
        self,
        api: TestClient,
        app: Phase1App,
    ) -> None:
        planner_run_id = await _planner_run_fixture(app)
        path = f"/api/v1/plans/{planner_run_id}/undo"
        before = copy.deepcopy(app.store)
        assert api.post(path, json={"invalid": "body"}).status_code == 401
        assert app.store == before
        assert api.post(
            path,
            json={"invalid": "body"},
            cookies={"commitmentos_session": SESSION_TOKEN},
        ).status_code == 403
        assert app.store == before

        response = api.post(
            path,
            json={"idempotency_key": "undo-route-once"},
            cookies={"commitmentos_session": SESSION_TOKEN},
            headers={"X-CSRF-Token": CSRF_SECRET},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert app.store.get("action_outbox", {}) == before.get("action_outbox", {})
        observation_id = response.json()["identifiers"]["observation_id"]
        assert (
            app.store["source_observations"][observation_id]["observation_type"]
            == "plan_undo_requested"
        )


class TestTaskRouteContracts:
    TASK_BODY = {
        "schema_version": "task_v1",
        "observation_id": "obs-unknown",
        "workflow_version": "reconciliation_workflow_v1",
        "dispatch_generation": 0,
        "trace_id": "trace-t",
    }

    def test_missing_bearer_is_rejected(self, api: TestClient, app: Phase1App) -> None:
        before = copy.deepcopy(app.store)
        response = api.post("/internal/tasks/reconcile-observation", json=self.TASK_BODY)
        assert response.status_code == 401
        assert app.store == before

    def test_invalid_token_is_rejected(self, api: TestClient, app: Phase1App) -> None:
        before = copy.deepcopy(app.store)
        response = api.post(
            "/internal/tasks/reconcile-observation",
            json=self.TASK_BODY,
            headers={"Authorization": "Bearer garbage"},
        )
        assert response.status_code == 403
        assert app.store == before

    def test_wrong_identity_is_rejected(self, api: TestClient, app: Phase1App) -> None:
        response = api.post(
            "/internal/tasks/reconcile-observation",
            json=self.TASK_BODY,
            headers={"Authorization": "Bearer valid:someone-else@test.iam.gserviceaccount.com"},
        )
        assert response.status_code == 403

    def test_valid_identity_reaches_the_command(self, api: TestClient, app: Phase1App) -> None:
        response = api.post(
            "/internal/tasks/reconcile-observation",
            json=self.TASK_BODY,
            headers={"Authorization": f"Bearer valid:{TASKS_SA}"},
        )
        assert response.status_code == 200
        assert response.json()["error_code"] == "observation_not_found"

    def test_auth_runs_before_body_validation(self, api: TestClient, app: Phase1App) -> None:
        """§16.3: an unauthenticated caller gets no schema feedback.

        A schema-invalid body without credentials must be rejected by the
        OIDC dependency (401), never by request validation (422) — otherwise
        unauthenticated callers can probe the task schema.
        """
        before = copy.deepcopy(app.store)
        response = api.post(
            "/internal/tasks/reconcile-observation",
            json={"totally": "wrong-shape"},
        )
        assert response.status_code == 401
        assert app.store == before

        response = api.post(
            "/internal/tasks/execute-calendar-action",
            json={"totally": "wrong-shape"},
            headers={"Authorization": "Bearer garbage"},
        )
        assert response.status_code == 403
        assert app.store == before

    def test_schema_validation_applies_only_after_valid_auth(
        self, api: TestClient, app: Phase1App
    ) -> None:
        response = api.post(
            "/internal/tasks/reconcile-observation",
            json={"totally": "wrong-shape"},
            headers={"Authorization": f"Bearer valid:{TASKS_SA}"},
        )
        assert response.status_code == 422


class TestControlledRouteOrdering:
    async def test_session_and_csrf_run_before_body_validation(
        self, api: TestClient, app: Phase1App
    ) -> None:
        # No session + invalid body: rejected by the session dependency.
        response = api.post(
            "/api/v1/approvals/some-id/resolve",
            json={"totally": "wrong-shape"},
        )
        assert response.status_code == 401

        # Session but no CSRF + invalid body: rejected by the CSRF dependency.
        response = api.post(
            "/api/v1/approvals/some-id/resolve",
            json={"totally": "wrong-shape"},
            cookies={"commitmentos_session": SESSION_TOKEN},
        )
        assert response.status_code == 403

        # Fully authenticated with an invalid body: only now 422.
        response = api.post(
            "/api/v1/approvals/some-id/resolve",
            json={"totally": "wrong-shape"},
            cookies={"commitmentos_session": SESSION_TOKEN},
            headers={"X-CSRF-Token": CSRF_SECRET},
        )
        assert response.status_code == 422


class TestCandidateDashboardReads:
    """Phase 2: the commitment listing and source evidence view sit behind
    the server-side session; reads never leak across missing sessions."""

    async def test_listing_requires_session(self, api: TestClient) -> None:
        assert api.get("/api/v1/commitments").status_code == 401

    async def test_detail_requires_session(self, api: TestClient) -> None:
        assert api.get("/api/v1/commitments/whatever").status_code == 401

    async def test_listing_and_evidence_view_serve_candidate_data(
        self, api: TestClient, app: Phase1App
    ) -> None:
        await _pending_approval(app)  # seeds the golden commitment
        listing = api.get(
            "/api/v1/commitments",
            cookies={"commitmentos_session": SESSION_TOKEN},
        )
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert len(items) == 1
        assert items[0]["lifecycle_status"] == "awaiting_confirmation"

        commitment_id = items[0]["commitment_id"]
        detail = api.get(
            f"/api/v1/commitments/{commitment_id}",
            cookies={"commitmentos_session": SESSION_TOKEN},
        )
        assert detail.status_code == 200
        body = detail.json()
        assert body["commitment"]["commitment_id"] == commitment_id
        assert body["evidence"], "evidence view must expose the excerpt"
        assert body["evidence"][0]["excerpt"]
        # Minimal evidence only: references and excerpts, never bodies.
        assert "body" not in body["evidence"][0]

    async def test_detail_of_unknown_commitment_is_404(
        self, api: TestClient
    ) -> None:
        response = api.get(
            "/api/v1/commitments/nonexistent",
            cookies={"commitmentos_session": SESSION_TOKEN},
        )
        assert response.status_code == 404

    async def test_unknown_filter_value_is_rejected(self, api: TestClient) -> None:
        response = api.get(
            "/api/v1/commitments?lifecycle_status=bogus",
            cookies={"commitmentos_session": SESSION_TOKEN},
        )
        assert response.status_code == 400
