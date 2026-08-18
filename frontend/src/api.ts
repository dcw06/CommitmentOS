// API client for the CommitmentOS dashboard.
//
// Two modes share every view:
//  - live  (/app):  session-guarded reads under /api/v1, CSRF-guarded
//                   mutations (X-CSRF-Token echoed from GET /api/v1/me).
//  - demo  (/demo): the seeded read-only judge mode; reads come from the
//                   static /demo endpoints and every mutation control is
//                   hidden (the server rejects demo mutations regardless).

//  - sandbox (/sandbox): the interactive judge sandbox; the whole stack runs
//                   over an isolated in-memory twin per session, addressed by
//                   an explicit session header rather than a cookie.

export const DEMO_MODE = window.location.pathname.startsWith("/demo");
export const SANDBOX_MODE = window.location.pathname.startsWith("/sandbox");
export const BASENAME = SANDBOX_MODE ? "/sandbox" : DEMO_MODE ? "/demo" : "/app";

const LOCAL_TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

// ---------------------------------------------------------------------------
// View models shared by both modes
// ---------------------------------------------------------------------------

export interface OutcomeStat {
  label: string;
  value: string | number;
}

export interface TodayBlock {
  workBlockId: string;
  commitmentId: string;
  title: string;
  start: string;
  end: string;
  executionState: string;
  verifiedMinutes: number;
  durationMinutes: number | null;
}

export interface AtRiskItem {
  commitmentId: string;
  title: string;
  deadline: string;
  riskLevel: string;
  remainingMinutes: number | null;
}

export interface PendingApproval {
  approvalId: string;
  commitmentId: string;
  requestType: string;
  revision: number;
  createdAt: string;
  payload: Record<string, unknown>;
}

export interface FailureState {
  state: string;
  details: Record<string, unknown>;
}

export interface ControlsState {
  observationMode: string;
  automaticActionMode: string;
  controlEpoch: number;
  heldActions: number;
  inFlightActions: number;
}

export interface CandidateView {
  commitmentId: string;
  title: string;
  ownershipType: string;
  lifecycleStatus: string;
  deadline: string;
  confidence: number | null;
}

export interface TodayView {
  localDate: string;
  blocks: TodayBlock[];
  candidates: CandidateView[];
  atRisk: AtRiskItem[];
  approvals: PendingApproval[];
  failures: FailureState[];
  outcome: OutcomeStat[];
  controls: ControlsState | null;
}

export interface PortfolioView {
  plannerRunId: string;
  position: number;
  size: number;
  allocatedMinutes: number;
  projectedFinish: string | null;
  shortfallMinutes: number;
  sharedBufferMinutes: number;
}

export interface CommitmentSummary {
  commitmentId: string;
  title: string;
  ownershipType: string;
  lifecycleStatus: string;
  deadline: string | null;
  riskLevel: string;
  remainingMinutes: number | null;
  confirmedMinutes: number | null;
  verifiedMinutes: number | null;
  blockingStatus: string;
  portfolio: PortfolioView | null;
}

export interface WorkBlockView {
  workBlockId: string;
  scheduledStart: string;
  scheduledEnd: string;
  executionState: string;
  verifiedMinutes: number;
  planRevision: number | string;
  revision?: number;
  note?: string;
}

export interface EvidenceView {
  excerpt: string;
  confidence: number | null;
}

export interface CommitmentDetail {
  summary: CommitmentSummary;
  revision: number;
  deadlineSourceExpression: string;
  deadlineConfidence: number | null;
  beneficiary: string;
  explicitPriority: number;
  evidence: EvidenceView[];
  workBlocks: WorkBlockView[];
  activity: ActivityItem[];
  approvals: PendingApproval[];
}

export interface ActivityItem {
  eventType: string;
  summary: string;
  actor: string;
  createdAt: string | null;
  traceId: string;
  payload: Record<string, unknown>;
}

export interface ActivityPage {
  items: ActivityItem[];
  nextCursor: string | null;
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

let csrfToken = "";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function getJson(path: string): Promise<unknown> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (response.status === 401 && !DEMO_MODE) {
    window.location.assign("/auth/login");
    throw new ApiError(401, "redirecting to login");
  }
  if (!response.ok) {
    throw new ApiError(response.status, await safeDetail(response));
  }
  return response.json();
}

export async function postJson(
  path: string,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify(body),
  });
  if (response.status === 401 && !DEMO_MODE) {
    window.location.assign("/auth/login");
    throw new ApiError(401, "redirecting to login");
  }
  if (!response.ok) {
    throw new ApiError(response.status, await safeDetail(response));
  }
  return (await response.json()) as Record<string, unknown>;
}

async function safeDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown; error_code?: unknown };
    const detail = body.detail ?? body.error_code;
    return typeof detail === "string" ? detail : `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

export async function bootstrapSession(): Promise<void> {
  if (DEMO_MODE) return;
  const me = (await getJson("/api/v1/me")) as { csrf_token?: string };
  csrfToken = me.csrf_token ?? "";
}

export async function logout(): Promise<void> {
  await postJson("/auth/logout", {});
  window.location.assign("/auth/login");
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}

// ---------------------------------------------------------------------------
// Today
// ---------------------------------------------------------------------------

export async function fetchToday(): Promise<TodayView> {
  if (DEMO_MODE) return demoToday();

  const [todayRaw, statusRaw] = await Promise.all([
    getJson(`/api/v1/dashboard/today?timezone=${encodeURIComponent(LOCAL_TIMEZONE)}`),
    getJson("/api/v1/dashboard/system-status"),
  ]);
  const today = record(todayRaw);
  const status = record(statusRaw);
  const controls = record(status.controls);

  const blocks: TodayBlock[] = list(today.work_blocks).map((block) => ({
    workBlockId: str(block.work_block_id),
    commitmentId: str(block.commitment_id),
    title: str(block.title),
    start: str(block.scheduled_start),
    end: str(block.scheduled_end),
    executionState: str(block.execution_state),
    verifiedMinutes: num(block.verified_minutes) ?? 0,
    durationMinutes: num(block.duration_minutes),
  }));

  const strip = record(today.outcome_strip);

  return {
    localDate: str(today.local_date),
    blocks,
    candidates: list(today.newly_detected_candidates).map((item) => ({
      commitmentId: str(item.commitment_id),
      title: str(item.title),
      ownershipType: str(item.ownership_type),
      lifecycleStatus: str(item.lifecycle_status),
      deadline: str(item.deadline),
      confidence: num(item.deadline_confidence),
    })),
    atRisk: list(today.at_risk_commitments).map((item) => ({
      commitmentId: str(item.commitment_id),
      title: str(item.title),
      deadline: str(item.deadline),
      riskLevel: str(item.risk_level, "unknown"),
      remainingMinutes: num(item.remaining_minutes),
    })),
    approvals: list(today.pending_approvals).map((item) => ({
      approvalId: str(item.approval_id),
      commitmentId: str(item.commitment_id),
      requestType: str(item.request_type),
      revision: num(item.revision) ?? 0,
      createdAt: str(item.created_at),
      payload: record(item.payload),
    })),
    failures: list(today.visible_failure_states).map((item) => {
      const { state, ...details } = item;
      return { state: str(state), details };
    }),
    outcome: [
      { label: "Commitments kept feasible", value: num(strip.commitments_kept_feasible) ?? 0 },
      { label: "Work minutes reserved", value: num(strip.work_minutes_reserved) ?? 0 },
      { label: "Conflicts repaired automatically", value: num(strip.conflicts_repaired_automatically) ?? 0 },
      { label: "Unaffected blocks preserved", value: num(strip.unaffected_blocks_preserved) ?? 0 },
      { label: "Manual reschedules avoided", value: num(strip.manual_reschedules_avoided) ?? 0 },
    ],
    controls: {
      observationMode: str(controls.observation_mode, "unknown"),
      automaticActionMode: str(controls.automatic_action_mode, "unknown"),
      controlEpoch: num(controls.control_epoch) ?? 0,
      heldActions: num(status.held_action_count) ?? 0,
      inFlightActions: num(status.in_flight_action_count) ?? 0,
    },
  };
}

async function demoToday(): Promise<TodayView> {
  const today = record(await getJson("/demo/api/today"));
  const strip = record(today.outcome_strip);
  const monitoring = record(today.monitoring);
  return {
    localDate: "",
    blocks: list(today.today_blocks).map((block, index) => ({
      workBlockId: `demo-${index}`,
      commitmentId: "",
      title: str(block.commitment_title),
      start: str(block.start),
      end: str(block.end),
      executionState: str(block.execution_state),
      verifiedMinutes: 0,
      durationMinutes: null,
    })),
    candidates: [],
    atRisk: list(today.at_risk).map((item) => ({
      commitmentId: "",
      title: str(item.commitment_title ?? item.title),
      deadline: str(item.deadline),
      riskLevel: str(item.risk_level, "at_risk"),
      remainingMinutes: num(item.remaining_minutes),
    })),
    approvals: list(today.pending_decisions).map((item, index) => ({
      approvalId: `demo-${index}`,
      commitmentId: "",
      requestType: str(item.type),
      revision: 0,
      createdAt: "",
      payload: item,
    })),
    failures: [],
    outcome: [
      {
        label: "Commitments kept feasible",
        value: num(strip.commitments_kept_feasible) ?? 0,
      },
      { label: "Work minutes reserved", value: num(strip.work_minutes_reserved) ?? 0 },
      {
        label: "Conflicts repaired automatically",
        value: num(strip.conflicts_repaired_automatically) ?? 0,
      },
      {
        label: "Unaffected blocks preserved",
        value: num(strip.unaffected_blocks_preserved) ?? 0,
      },
      {
        label: "Manual reschedules avoided",
        value: num(strip.manual_reschedules_avoided) ?? 0,
      },
    ],
    controls: {
      observationMode: str(monitoring.observation_mode, "active"),
      automaticActionMode: str(monitoring.automatic_action_mode, "enabled"),
      controlEpoch: 0,
      heldActions: num(monitoring.held_actions) ?? 0,
      inFlightActions: 0,
    },
  };
}

// ---------------------------------------------------------------------------
// Commitments
// ---------------------------------------------------------------------------

export async function fetchCommitments(): Promise<CommitmentSummary[]> {
  if (DEMO_MODE) {
    return list(await getJson("/demo/api/commitments")).map((item, index) => ({
      commitmentId: `demo-${index}`,
      title: str(item.title),
      ownershipType: str(item.ownership_type),
      lifecycleStatus: str(item.lifecycle_status),
      deadline: str(item.deadline) || null,
      riskLevel: str(item.risk_level, "unknown"),
      remainingMinutes: num(item.remaining_minutes),
      confirmedMinutes: num(item.confirmed_minutes),
      verifiedMinutes: num(item.verified_completed_minutes),
      blockingStatus: str(item.blocking_status, "clear"),
      portfolio: portfolio(record(item.portfolio)),
    }));
  }
  const page = record(await getJson("/api/v1/commitments?limit=50"));
  return list(page.items).map((item) => {
    const projection = record(item.projection);
    const projectionCurrent = projection.current !== false;
    const deadline = record(item.deadline);
    return {
      commitmentId: str(item.commitment_id),
      title: str(item.title),
      ownershipType: str(item.ownership_type),
      lifecycleStatus: str(item.lifecycle_status),
      deadline: str(deadline.value ?? item.deadline) || null,
      riskLevel: projectionCurrent
        ? str(projection.risk_level ?? item.risk_level, "unknown")
        : "unknown",
      remainingMinutes: projectionCurrent
        ? num(projection.remaining_minutes ?? item.remaining_minutes)
        : null,
      confirmedMinutes: num(record(item.effort).confirmed_minutes),
      verifiedMinutes: null,
      blockingStatus: str(projection.blocking_status, "clear"),
      portfolio: portfolio(record(item.portfolio)),
    };
  });
}

export async function fetchCommitmentDetail(
  commitmentId: string,
): Promise<CommitmentDetail> {
  if (DEMO_MODE) {
    const index = Number(commitmentId.replace("demo-", "")) || 0;
    const items = list(await getJson("/demo/api/commitments"));
    const item = items[index] ?? {};
    return {
      summary: {
        commitmentId,
        title: str(item.title),
        ownershipType: str(item.ownership_type),
        lifecycleStatus: str(item.lifecycle_status),
        deadline: str(item.deadline) || null,
        riskLevel: str(item.risk_level, "unknown"),
        remainingMinutes: num(item.remaining_minutes),
        confirmedMinutes: num(item.confirmed_minutes),
        verifiedMinutes: num(item.verified_completed_minutes),
        blockingStatus: str(item.blocking_status, "clear"),
        portfolio: portfolio(record(item.portfolio)),
      },
      revision: 0,
      deadlineSourceExpression: str(item.deadline_source_expression),
      deadlineConfidence: num(item.deadline_confidence),
      beneficiary: str(item.beneficiary),
      explicitPriority: num(item.explicit_priority) ?? 0,
      evidence: (Array.isArray(item.evidence_excerpts) ? item.evidence_excerpts : [])
        .map((excerpt) => ({ excerpt: str(excerpt), confidence: null })),
      workBlocks: list(item.work_blocks).map((block, blockIndex) => ({
        workBlockId: `demo-${blockIndex}`,
        scheduledStart: `${str(block.day)} ${str(block.start)}`,
        scheduledEnd: str(block.end),
        executionState: str(block.execution_state),
        verifiedMinutes: num(block.verified_minutes) ?? 0,
        planRevision: "",
        note: str(block.note) || undefined,
      })),
      activity: [],
      approvals: [],
    };
  }

  const detail = record(await getJson(`/api/v1/commitments/${commitmentId}`));
  const commitment = record(detail.commitment);
  const deadline = record(commitment.deadline);
  const effort = record(commitment.effort);
  const projection = record(commitment.projection);
  const projectionCurrent = projection.current !== false;
  const workBlocks = list(detail.work_blocks).map((block) => ({
    workBlockId: str(block.work_block_id),
    scheduledStart: str(block.scheduled_start),
    scheduledEnd: str(block.scheduled_end),
    executionState: str(block.execution_state),
    verifiedMinutes: num(block.verified_minutes) ?? 0,
    planRevision: num(block.plan_revision) ?? 0,
    revision: num(block.revision) ?? 0,
  }));
  const verified = workBlocks.reduce((total, b) => total + b.verifiedMinutes, 0);
  return {
    summary: {
      commitmentId: str(commitment.commitment_id),
      title: str(commitment.title),
      ownershipType: str(commitment.ownership_type),
      lifecycleStatus: str(commitment.lifecycle_status),
      deadline: str(deadline.value) || null,
      riskLevel: projectionCurrent ? str(projection.risk_level, "unknown") : "unknown",
      remainingMinutes: projectionCurrent ? num(projection.remaining_minutes) : null,
      confirmedMinutes: num(effort.confirmed_minutes),
      verifiedMinutes: projectionCurrent
        ? num(projection.verified_completed_minutes) ?? verified
        : verified,
      blockingStatus: projectionCurrent
        ? str(projection.blocking_status, "clear")
        : "unknown",
      portfolio: portfolio(record(detail.portfolio)),
    },
    revision: num(commitment.revision) ?? 0,
    deadlineSourceExpression: str(deadline.source_expression),
    deadlineConfidence: num(deadline.confidence),
    beneficiary: str(commitment.beneficiary),
    explicitPriority: num(commitment.explicit_priority) ?? 0,
    evidence: list(detail.evidence).map((item) => ({
      excerpt: str(item.excerpt),
      confidence: num(item.confidence),
    })),
    workBlocks,
    activity: list(detail.activity).map((event) => ({
      eventType: str(event.event_type),
      summary: str(event.summary),
      actor: str(event.actor),
      createdAt: str(event.created_at) || null,
      traceId: str(event.trace_id),
      payload: record(event.payload),
    })),
    approvals: list(detail.pending_approvals).map(approval),
  };
}

// ---------------------------------------------------------------------------
// Activity
// ---------------------------------------------------------------------------

export async function fetchActivity(cursor: string | null): Promise<ActivityPage> {
  if (DEMO_MODE) {
    const items = list(await getJson("/demo/api/activity"));
    return {
      items: items.map((item) => ({
        eventType: str(item.event),
        summary: str(item.summary),
        actor: "seeded",
        createdAt: null,
        traceId: "",
        payload: item,
      })),
      nextCursor: null,
    };
  }
  const query = cursor ? `?limit=50&cursor=${encodeURIComponent(cursor)}` : "?limit=50";
  const page = record(await getJson(`/api/v1/dashboard/activity${query}`));
  return {
    items: list(page.items).map((item) => ({
      eventType: str(item.event_type),
      summary: str(item.summary),
      actor: str(item.actor),
      createdAt: str(item.created_at) || null,
      traceId: str(item.trace_id),
      payload: record(item.payload),
    })),
    nextCursor: str(page.next_cursor) || null,
  };
}

// ---------------------------------------------------------------------------
// Mutations (live mode only; the UI hides these in demo mode)
// ---------------------------------------------------------------------------

function idempotencyKey(prefix: string): string {
  const entropy = crypto.getRandomValues(new Uint32Array(2));
  return `${prefix}-${Date.now()}-${entropy[0].toString(16)}${entropy[1].toString(16)}`;
}

export interface ApprovalDecision {
  decision: "approve" | "reject";
  confirmedMinutes?: number;
  deadline?: string;
  ownershipType?: string;
  choice?: string;
  reason?: string;
}

export async function resolveApproval(
  approval: PendingApproval,
  decision: ApprovalDecision,
): Promise<void> {
  const body: Record<string, unknown> = {
    decision: decision.decision,
    expected_revision: approval.revision,
  };
  if (decision.confirmedMinutes !== undefined) {
    body.confirmed_minutes = decision.confirmedMinutes;
  }
  if (decision.deadline) body.deadline = decision.deadline;
  if (decision.ownershipType) body.ownership_type = decision.ownershipType;
  if (decision.choice) body.choice = decision.choice;
  if (decision.reason) body.reason = decision.reason;
  await postJson(`/api/v1/approvals/${approval.approvalId}/resolve`, body);
}

export async function recordCheckIn(
  workBlockId: string,
  expectedRevision: number,
  verifiedMinutes: number,
): Promise<void> {
  await postJson(`/api/v1/work-blocks/${workBlockId}/check-in`, {
    idempotency_key: idempotencyKey("ui-checkin"),
    completed: true,
    verified_minutes: verifiedMinutes,
    checked_in_at: new Date().toISOString(),
    expected_revision: expectedRevision,
  });
}

export async function completeCommitment(
  commitmentId: string,
  expectedRevision: number,
  note: string,
): Promise<void> {
  await postJson(`/api/v1/commitments/${commitmentId}/complete`, {
    idempotency_key: idempotencyKey("ui-complete"),
    completed_at: new Date().toISOString(),
    note: note || null,
    expected_revision: expectedRevision,
  });
}

export async function reopenCommitment(
  commitmentId: string,
  expectedRevision: number,
): Promise<void> {
  await postJson(`/api/v1/commitments/${commitmentId}/reopen`, {
    expected_revision: expectedRevision,
  });
}

export async function setCommitmentPriority(
  commitmentId: string,
  expectedRevision: number,
  priority: number,
): Promise<void> {
  await postJson(`/api/v1/commitments/${commitmentId}/priority`, {
    expected_revision: expectedRevision,
    priority,
  });
}

export async function changeCommitmentLifecycle(
  commitmentId: string,
  expectedRevision: number,
  targetStatus: "paused" | "active" | "dismissed",
): Promise<void> {
  await postJson(`/api/v1/commitments/${commitmentId}/lifecycle`, {
    expected_revision: expectedRevision,
    target_status: targetStatus,
  });
}

export async function fetchSystemStatus(): Promise<ControlsState> {
  const status = record(await getJson("/api/v1/dashboard/system-status"));
  const controls = record(status.controls);
  return {
    observationMode: str(controls.observation_mode, "unknown"),
    automaticActionMode: str(controls.automatic_action_mode, "unknown"),
    controlEpoch: num(controls.control_epoch) ?? 0,
    heldActions: num(status.held_action_count) ?? 0,
    inFlightActions: num(status.in_flight_action_count) ?? 0,
  };
}

function approval(item: Record<string, unknown>): PendingApproval {
  return {
    approvalId: str(item.approval_id),
    commitmentId: str(item.commitment_id),
    requestType: str(item.request_type),
    revision: num(item.revision) ?? 0,
    createdAt: str(item.created_at),
    payload: record(item.payload),
  };
}

function portfolio(item: Record<string, unknown>): PortfolioView | null {
  const plannerRunId = str(item.planner_run_id);
  if (!plannerRunId) return null;
  return {
    plannerRunId,
    position: num(item.portfolio_position) ?? 0,
    size: num(item.portfolio_size) ?? 0,
    allocatedMinutes: num(item.allocated_work_minutes) ?? 0,
    projectedFinish: str(item.projected_finish) || null,
    shortfallMinutes: num(item.shortfall_minutes) ?? 0,
    sharedBufferMinutes: num(item.shared_buffer_minutes) ?? 0,
  };
}

export async function changeControl(
  controlName: "monitoring" | "automatic_actions",
  targetMode: "enabled" | "paused",
  expectedControlEpoch: number,
): Promise<void> {
  await postJson("/api/v1/controls/change", {
    control_name: controlName,
    target_mode: targetMode,
    reason: `dashboard ${controlName} ${targetMode}`,
    expected_control_epoch: expectedControlEpoch,
  });
}
