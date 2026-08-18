import { useState } from "react";
import { Link } from "react-router-dom";

import {
  changeControl,
  DEMO_MODE,
  fetchToday,
  PendingApproval,
  resolveApproval,
  TodayView,
} from "../api";
import {
  Badge,
  Card,
  Empty,
  formatDateTime,
  formatTime,
  minutesLabel,
  useAction,
  usePolling,
} from "../ui";

const APPROVAL_TITLES: Record<string, string> = {
  effort_confirmation: "Confirm effort before planning",
  initial_plan_approval: "Approve the first Calendar plan",
  action_approval: "Approve an out-of-policy repair",
  identity_confirmation: "Confirm an ambiguous commitment",
  deadline_change_confirmation: "Accept a proposed deadline change",
  deadline_required_confirmation: "Add the missing deadline",
  retraction_confirmation: "Confirm an explicit retraction",
  calendar_invalid_move_decision: "Decide on an invalid manual move",
  calendar_user_deleted_decision: "Decide what happens to a deleted block",
};

const CHOICE_LABELS: Record<string, string> = {
  restore_approved_slot: "Restore the approved slot",
  reschedule_safely: "Reschedule to a safe slot",
  pause_commitment: "Pause the commitment",
  reschedule_unfinished: "Reschedule the unfinished minutes",
  record_completed: "Record the work as completed",
};

const OWNERSHIP_LABELS: Record<string, string> = {
  my_commitment: "My commitment — I promised this",
  request_to_me: "A request to me — not yet accepted",
  commitment_to_me: "Someone else's promise to me",
};

const CALENDAR_DECISION_TYPES = new Set([
  "calendar_invalid_move_decision",
  "calendar_user_deleted_decision",
]);

export function ApprovalCard({
  approval,
  refresh,
}: {
  approval: PendingApproval;
  refresh: () => void;
}) {
  const { busy, error, run } = useAction(refresh);
  const proposed =
    typeof approval.payload.proposed_minutes === "number"
      ? approval.payload.proposed_minutes
      : 60;
  const [minutes, setMinutes] = useState(proposed);
  const [deadline, setDeadline] = useState("");
  const reasons = Array.isArray(approval.payload.reason_codes)
    ? approval.payload.reason_codes.join(", ")
    : "";
  const previousRejectionReason =
    typeof approval.payload.previous_rejection_reason === "string"
      ? approval.payload.previous_rejection_reason
      : "";
  const title = APPROVAL_TITLES[approval.requestType] ?? approval.requestType;

  // Calendar decisions carry their valid choices in the approval payload;
  // the backend rejects an approve without one of exactly these values.
  const isCalendarDecision = CALENDAR_DECISION_TYPES.has(approval.requestType);
  const choiceOptions = isCalendarDecision
    ? ((approval.payload.options as string[] | undefined) ?? [])
    : [];
  const [choice, setChoice] = useState<string>(choiceOptions[0] ?? "");

  // An ambiguous identity proposal cannot be approved without a confirmed
  // ownership value (confirmed_ownership_required).
  const proposal = approval.payload.proposal as
    | Record<string, unknown>
    | undefined;
  const needsOwnership =
    approval.requestType === "identity_confirmation" &&
    proposal?.ownership_type === "ambiguous";
  const [ownership, setOwnership] = useState<string>("my_commitment");
  const deadlineProposal = proposal?.deadline as
    | Record<string, unknown>
    | undefined;
  const deadlineExpression =
    typeof deadlineProposal?.source_expression === "string"
      ? deadlineProposal.source_expression
      : "the proposed date";
  const needsDeadline =
    approval.requestType === "deadline_required_confirmation";
  const validMinutes =
    Number.isInteger(minutes) &&
    minutes >= 30 &&
    minutes <= 2400 &&
    minutes % 15 === 0;
  const validDeadline =
    Boolean(deadline) && !Number.isNaN(new Date(deadline).getTime());

  const approveDecision = () => {
    const decision: Parameters<typeof resolveApproval>[1] = {
      decision: "approve",
    };
    if (approval.requestType === "effort_confirmation") {
      decision.confirmedMinutes = minutes;
    }
    if (needsDeadline && validDeadline) {
      decision.deadline = new Date(deadline).toISOString();
    }
    if (isCalendarDecision) decision.choice = choice;
    if (needsOwnership) decision.ownershipType = ownership;
    return resolveApproval(approval, decision);
  };

  return (
    <div className="approval">
      <div className="kind">{title}</div>
      <div className="meta">
        {approval.requestType === "effort_confirmation" &&
          `Proposed estimate: ${minutesLabel(proposed)}. Confirm or edit before the first plan is computed.`}
        {approval.requestType === "initial_plan_approval" &&
          "The deterministic planner produced a constraint-safe portfolio plan; nothing reaches Calendar before this approval."}
        {approval.requestType === "action_approval" &&
          `Policy escalation${reasons ? `: ${reasons}` : ""}. The repair mutates nothing until approved.`}
        {approval.requestType === "identity_confirmation" &&
          (needsOwnership
            ? "The interpreter could not resolve who owns this commitment. Pick the ownership that matches the thread, or reject to dismiss the span."
            : "Confirm whether this detected commitment is real; rejecting dismisses the source span durably.")}
        {approval.requestType === "deadline_change_confirmation" &&
          `A counterparty proposed “${deadlineExpression}”. Your current deadline remains binding unless you accept this change.`}
        {approval.requestType === "deadline_required_confirmation" &&
          "The commitment has no usable deadline. Choose one before it can be created or scheduled."}
        {approval.requestType === "retraction_confirmation" &&
          "This message appears to retract an existing commitment. Confirm to cancel it, or reject to keep it open."}
        {approval.requestType === "calendar_invalid_move_decision" &&
          "A manual Calendar move violates hard constraints. The observed event is preserved until you choose."}
        {approval.requestType === "calendar_user_deleted_decision" &&
          "You deleted an app-owned block. Nothing is recreated until you decide what the deletion meant."}
        {!(approval.requestType in APPROVAL_TITLES) &&
          JSON.stringify(approval.payload).slice(0, 240)}
        {previousRejectionReason &&
          ` Your previous rejection reason: ${previousRejectionReason}.`}
      </div>
      {DEMO_MODE ? (
        <Badge value="pending" />
      ) : (
        <div className="inline-form">
          {approval.requestType === "effort_confirmation" && (
            <input
              type="number"
              min={30}
              step={15}
              value={minutes}
              onChange={(event) => setMinutes(Number(event.target.value))}
              aria-label="confirmed minutes"
            />
          )}
          {needsDeadline && (
            <input
              type="datetime-local"
              value={deadline}
              onChange={(event) => setDeadline(event.target.value)}
              aria-label="commitment deadline"
            />
          )}
          {isCalendarDecision && (
            <select
              value={choice}
              onChange={(event) => setChoice(event.target.value)}
              aria-label="decision choice"
            >
              {choiceOptions.map((option) => (
                <option key={option} value={option}>
                  {CHOICE_LABELS[option] ?? option}
                </option>
              ))}
            </select>
          )}
          {needsOwnership && (
            <select
              value={ownership}
              onChange={(event) => setOwnership(event.target.value)}
              aria-label="confirmed ownership"
            >
              {Object.entries(OWNERSHIP_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          )}
          <button
            className="primary"
            disabled={
              busy ||
              (isCalendarDecision && !choice) ||
              (approval.requestType === "effort_confirmation" && !validMinutes) ||
              (needsDeadline && !validDeadline)
            }
            onClick={() => run(approveDecision)}
          >
            {approval.requestType === "effort_confirmation"
              ? `Confirm ${minutesLabel(minutes)}`
              : approval.requestType === "deadline_change_confirmation"
                ? "Accept deadline"
              : needsDeadline
                ? "Confirm deadline"
              : "Approve"}
          </button>
          <button
            className="danger"
            disabled={busy}
            onClick={() =>
              run(() =>
                resolveApproval(approval, {
                  decision: "reject",
                  reason: "rejected from dashboard",
                }),
              )
            }
          >
            Reject
          </button>
          {error ? <span className="error-note">{error}</span> : null}
        </div>
      )}
    </div>
  );
}

function Controls({ view, refresh }: { view: TodayView; refresh: () => void }) {
  const { busy, error, run } = useAction(refresh);
  const controls = view.controls;
  if (!controls) return null;
  const monitoringPaused = controls.observationMode === "paused";
  const actionsPaused = controls.automaticActionMode === "paused";
  const toggleAutomaticActions = async () => {
    if (
      actionsPaused &&
      !window.confirm(
        "Resume automatic Calendar actions? Every held action will be revalidated against current commitment, plan, control, and Calendar state before execution.",
      )
    ) {
      return;
    }
    await changeControl(
      "automatic_actions",
      actionsPaused ? "enabled" : "paused",
      controls.controlEpoch,
    );
  };
  return (
    <Card
      title="Execution controls"
      note="Pausing automatic actions holds every not-yet-started Calendar mutation; resuming revalidates held intent before anything executes."
    >
      <div className="row-list">
        <div className="row">
          <div className="grow">
            <div className="title">Monitoring</div>
            <div className="sub">Observation processing for new source signals</div>
          </div>
          <Badge value={controls.observationMode} />
          {!DEMO_MODE && (
            <button
              disabled={busy}
              onClick={() =>
                run(() =>
                  changeControl(
                    "monitoring",
                    monitoringPaused ? "enabled" : "paused",
                    controls.controlEpoch,
                  ),
                )
              }
            >
              {monitoringPaused ? "Resume" : "Pause"}
            </button>
          )}
        </div>
        <div className="row">
          <div className="grow">
            <div className="title">Automatic actions</div>
            <div className="sub">
              {controls.heldActions > 0
                ? `${controls.heldActions} action(s) held by control`
                : controls.inFlightActions > 0
                  ? `${controls.inFlightActions} action(s) in flight`
                  : "Calendar mutations within policy execute automatically"}
            </div>
          </div>
          <Badge value={controls.automaticActionMode} />
          {!DEMO_MODE && (
            <button
              disabled={busy}
              onClick={() => run(toggleAutomaticActions)}
            >
              {actionsPaused ? "Resume" : "Pause"}
            </button>
          )}
        </div>
      </div>
      {error ? <div className="error-note">{error}</div> : null}
    </Card>
  );
}

export function TodayPage() {
  const { data, error, refresh } = usePolling(fetchToday, 8000);
  if (!data) return <div className="loading">{error ?? "Loading today…"}</div>;

  return (
    <>
      <div className="outcome-strip">
        {data.outcome.map((stat) => (
          <div className="outcome-stat" key={stat.label}>
            <div className="value">{stat.value}</div>
            <div className="label">{stat.label}</div>
          </div>
        ))}
      </div>

      {data.failures.length > 0 && (
        <Card
          title="Needs attention"
          note="Failure truth is never hidden behind an on-track badge."
        >
          {data.failures.map((failure, index) => (
            <div className="failure" key={`${failure.state}-${index}`}>
              <code>{failure.state}</code>
              {Object.keys(failure.details).length > 0 &&
                ` — ${JSON.stringify(failure.details)}`}
            </div>
          ))}
        </Card>
      )}

      {data.approvals.length > 0 && (
        <Card
          title="Pending decisions"
          note="Bounded runs terminate here and continue from your durable decision."
        >
          {data.approvals.map((approval) => (
            <ApprovalCard key={approval.approvalId} approval={approval} refresh={refresh} />
          ))}
        </Card>
      )}

      <Card title="Today's work blocks">
        <div className="row-list">
          {data.blocks.length === 0 && <Empty>No app-owned blocks scheduled today.</Empty>}
          {data.blocks.map((block) => (
            <div className="row" key={block.workBlockId}>
              <span className="time-range">
                {DEMO_MODE ? `${block.start}–${block.end}` : `${formatTime(block.start)}–${formatTime(block.end)}`}
              </span>
              <div className="grow">
                <div className="title">
                  {block.title ||
                    (block.commitmentId ? (
                      <Link
                        className="commitment-link"
                        to={`/commitments/${block.commitmentId}`}
                      >
                        Work block · {minutesLabel(block.durationMinutes)}
                      </Link>
                    ) : (
                      "Work block"
                    ))}
                </div>
                {block.verifiedMinutes > 0 && (
                  <div className="sub">{block.verifiedMinutes} verified minutes</div>
                )}
              </div>
              <Badge value={block.executionState} />
            </div>
          ))}
        </div>
      </Card>

      <Card
        title="Newly detected candidates"
        note="Evidence-backed candidates remain visible until you confirm or dismiss them."
      >
        <div className="row-list">
          {data.candidates.length === 0 && (
            <Empty>No unconfirmed candidates right now.</Empty>
          )}
          {data.candidates.map((candidate) => (
            <div className="row" key={candidate.commitmentId}>
              <div className="grow">
                <div className="title">
                  <Link
                    className="commitment-link"
                    to={`/commitments/${candidate.commitmentId}`}
                  >
                    {candidate.title}
                  </Link>
                </div>
                <div className="sub">
                  {candidate.ownershipType.replaceAll("_", " ")} · due{" "}
                  {formatDateTime(candidate.deadline)}
                  {candidate.confidence !== null &&
                    ` · ${Math.round(candidate.confidence * 100)}% deadline confidence`}
                </div>
              </div>
              <Badge value={candidate.lifecycleStatus} />
            </div>
          ))}
        </div>
      </Card>

      <Card title="At risk">
        <div className="row-list">
          {data.atRisk.length === 0 && (
            <Empty>Every active commitment is currently feasible.</Empty>
          )}
          {data.atRisk.map((item) => (
            <div className="row" key={item.commitmentId || item.title}>
              <div className="grow">
                <div className="title">
                  {item.commitmentId && !DEMO_MODE ? (
                    <Link className="commitment-link" to={`/commitments/${item.commitmentId}`}>
                      {item.title}
                    </Link>
                  ) : (
                    item.title
                  )}
                </div>
                <div className="sub">
                  {minutesLabel(item.remainingMinutes)} remaining · due {item.deadline}
                </div>
              </div>
              <Badge value={item.riskLevel} />
            </div>
          ))}
        </div>
      </Card>

      <Controls view={data} refresh={refresh} />
    </>
  );
}
