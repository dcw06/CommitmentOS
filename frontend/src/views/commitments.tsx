import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  completeCommitment,
  changeCommitmentLifecycle,
  DEMO_MODE,
  fetchCommitmentDetail,
  fetchCommitments,
  recordCheckIn,
  reopenCommitment,
  setCommitmentPriority,
  WorkBlockView,
} from "../api";
import {
  Badge,
  Card,
  Empty,
  formatDateTime,
  formatRange,
  minutesLabel,
  useAction,
  usePolling,
} from "../ui";
import { ApprovalCard } from "./today";

export function CommitmentsPage() {
  const { data, error } = usePolling(fetchCommitments, 15000);
  if (!data) return <div className="loading">{error ?? "Loading commitments…"}</div>;

  return (
    <Card
      title="Commitments"
      note="The durable ledger: evidence-backed promises with ownership, deadlines, verified progress, and risk."
    >
      <div className="row-list">
        {data.length === 0 && <Empty>No commitments recorded yet.</Empty>}
        {data.map((item) => (
          <Link
            key={item.commitmentId}
            className="commitment-link"
            to={`/commitments/${item.commitmentId}`}
          >
            <div className="row">
              <div className="grow">
                <div className="title">{item.title}</div>
                <div className="sub">
                  {item.ownershipType.replaceAll("_", " ")}
                  {item.deadline &&
                    ` · due ${DEMO_MODE ? item.deadline : formatDateTime(item.deadline)}`}
                  {item.remainingMinutes !== null &&
                    ` · ${minutesLabel(item.remainingMinutes)} remaining`}
                  {item.portfolio &&
                    ` · ${minutesLabel(item.portfolio.allocatedMinutes)} allocated · ${minutesLabel(item.portfolio.sharedBufferMinutes)} shared buffer`}
                </div>
                {item.portfolio && (
                  <div className="sub">
                    Portfolio {item.portfolio.position}/{item.portfolio.size} · projected{" "}
                    {formatDateTime(item.portfolio.projectedFinish) || "—"}
                    {item.portfolio.shortfallMinutes > 0 &&
                      ` · ${minutesLabel(item.portfolio.shortfallMinutes)} shortfall`}
                  </div>
                )}
              </div>
              <Badge value={item.lifecycleStatus} />
              {item.riskLevel !== "unknown" && <Badge value={item.riskLevel} />}
            </div>
          </Link>
        ))}
      </div>
    </Card>
  );
}

function CheckInRow({
  block,
  refresh,
}: {
  block: WorkBlockView & { revision?: number };
  refresh: () => void;
}) {
  const { busy, error, run } = useAction(refresh);
  const duration = defaultMinutes(block);
  const [minutes, setMinutes] = useState(duration);
  const canCheckIn =
    !DEMO_MODE &&
    (block.executionState === "awaiting_check_in" || block.executionState === "active");

  return (
    <div className="row">
      <span className="time-range">
        {DEMO_MODE
          ? block.scheduledStart
          : formatRange(block.scheduledStart, block.scheduledEnd)}
      </span>
      <div className="grow">
        <div className="sub">
          {block.verifiedMinutes > 0
            ? `${block.verifiedMinutes} verified minutes`
            : "no verified minutes yet"}
          {block.note ? ` — ${block.note}` : ""}
        </div>
        {error ? <div className="error-note">{error}</div> : null}
      </div>
      <Badge value={block.executionState} />
      {canCheckIn && (
        <span className="inline-form">
          <input
            type="number"
            min={0}
            step={15}
            value={minutes}
            onChange={(event) => setMinutes(Number(event.target.value))}
            aria-label="verified minutes"
          />
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              run(() => recordCheckIn(block.workBlockId, block.revision ?? 0, minutes))
            }
          >
            Check in
          </button>
        </span>
      )}
    </div>
  );
}

function defaultMinutes(block: WorkBlockView): number {
  const start = new Date(block.scheduledStart).getTime();
  const end = new Date(block.scheduledEnd).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return 60;
  return Math.max(Math.round((end - start) / 60000), 0);
}

export function CommitmentDetailPage() {
  const { commitmentId = "" } = useParams();
  const { data, error, refresh } = usePolling(
    () => fetchCommitmentDetail(commitmentId),
    10000,
  );
  const completion = useAction(refresh);
  const changes = useAction(refresh);
  const [note, setNote] = useState("");
  const [priority, setPriority] = useState<number | null>(null);

  if (!data) return <div className="loading">{error ?? "Loading commitment…"}</div>;
  const { summary } = data;
  const confirmed = summary.confirmedMinutes ?? 0;
  const verified = summary.verifiedMinutes ?? 0;
  const progress = confirmed > 0 ? Math.min(verified / confirmed, 1) : 0;
  const completed = summary.lifecycleStatus === "completed";
  const terminal = ["completed", "dismissed", "canceled"].includes(
    summary.lifecycleStatus,
  );
  const canPause = ["active", "in_progress"].includes(summary.lifecycleStatus);
  const canResume = summary.lifecycleStatus === "paused";
  const canDismiss = ["candidate", "awaiting_confirmation", "paused"].includes(
    summary.lifecycleStatus,
  );
  const canComplete = [
    "active",
    "in_progress",
    "completion_candidate",
    "paused",
  ].includes(summary.lifecycleStatus);

  const lifecycleChange = (
    target: "paused" | "active" | "dismissed",
  ) => {
    if (
      target === "dismissed" &&
      !window.confirm(
        "Dismiss this commitment? Its pending confirmations will be superseded and it will leave the active portfolio.",
      )
    ) {
      return Promise.resolve();
    }
    return changeCommitmentLifecycle(summary.commitmentId, data.revision, target);
  };

  return (
    <>
      <p>
        <Link className="back-link" to="/commitments">
          ← All commitments
        </Link>
      </p>
      <Card title={summary.title}>
        <div className="detail-grid">
          <div>
            <div className="k">Lifecycle</div>
            <div className="v">
              <Badge value={summary.lifecycleStatus} />
            </div>
          </div>
          <div>
            <div className="k">Ownership</div>
            <div className="v">{summary.ownershipType.replaceAll("_", " ")}</div>
          </div>
          {data.beneficiary && (
            <div>
              <div className="k">Beneficiary</div>
              <div className="v">{data.beneficiary}</div>
            </div>
          )}
          <div>
            <div className="k">Deadline</div>
            <div className="v">
              {summary.deadline
                ? DEMO_MODE
                  ? summary.deadline
                  : formatDateTime(summary.deadline)
                : "—"}
            </div>
          </div>
          <div>
            <div className="k">Confirmed effort</div>
            <div className="v">{minutesLabel(summary.confirmedMinutes)}</div>
          </div>
          <div>
            <div className="k">Verified progress</div>
            <div className="v">{minutesLabel(verified)}</div>
          </div>
          <div>
            <div className="k">Priority</div>
            <div className="v">{data.explicitPriority}</div>
          </div>
          <div>
            <div className="k">Risk</div>
            <div className="v"><Badge value={summary.riskLevel} /></div>
          </div>
          <div>
            <div className="k">Remaining effort</div>
            <div className="v">{minutesLabel(summary.remainingMinutes)}</div>
          </div>
          <div>
            <div className="k">Blocking</div>
            <div className="v"><Badge value={summary.blockingStatus} /></div>
          </div>
        </div>
        {confirmed > 0 && (
          <>
            <div className="progress-track">
              <div className="progress-fill" style={{ width: `${progress * 100}%` }} />
            </div>
            <div className="sub" style={{ color: "var(--text-muted)", fontSize: 12.5 }}>
              Only explicitly verified minutes count — elapsed time never reduces
              remaining effort.
            </div>
          </>
        )}
        {data.deadlineSourceExpression && (
          <p className="section-note" style={{ marginTop: 10 }}>
            Deadline interpreted from “{data.deadlineSourceExpression}”
            {data.deadlineConfidence !== null &&
              ` (confidence ${Math.round(data.deadlineConfidence * 100)}%)`}
          </p>
        )}
      </Card>

      {summary.portfolio && (
        <Card
          title="Portfolio allocation"
          note={`Published planner run ${summary.portfolio.plannerRunId.slice(0, 12)}…`}
        >
          <div className="detail-grid">
            <div>
              <div className="k">Stable order</div>
              <div className="v">
                {summary.portfolio.position} of {summary.portfolio.size}
              </div>
            </div>
            <div>
              <div className="k">Allocated work</div>
              <div className="v">{minutesLabel(summary.portfolio.allocatedMinutes)}</div>
            </div>
            <div>
              <div className="k">Projected finish</div>
              <div className="v">
                {formatDateTime(summary.portfolio.projectedFinish) || "—"}
              </div>
            </div>
            <div>
              <div className="k">Shortfall</div>
              <div className="v">{minutesLabel(summary.portfolio.shortfallMinutes)}</div>
            </div>
            <div>
              <div className="k">Shared buffer</div>
              <div className="v">{minutesLabel(summary.portfolio.sharedBufferMinutes)}</div>
            </div>
          </div>
        </Card>
      )}

      {data.approvals.length > 0 && (
        <Card
          title="Pending confirmation"
          note="Confirm or reject this commitment without leaving its evidence view."
        >
          {data.approvals.map((approval) => (
            <ApprovalCard
              key={approval.approvalId}
              approval={approval}
              refresh={refresh}
            />
          ))}
        </Card>
      )}

      {!DEMO_MODE && (
        <Card
          title="Commitment controls"
          note="Priority is an explicit portfolio tie-breaker; lower numbers plan first. Reopening is available only after completion."
        >
          <div className="inline-form">
            <input
              type="number"
              min={-100}
              max={100}
              disabled={terminal}
              aria-label="Commitment priority"
              value={priority ?? data.explicitPriority}
              onChange={(event) => setPriority(Number(event.target.value))}
            />
            {!terminal && (
              <button
                disabled={changes.busy}
                onClick={() =>
                  changes.run(() =>
                    setCommitmentPriority(
                      summary.commitmentId,
                      data.revision,
                      priority ?? data.explicitPriority,
                    ),
                  )
                }
              >
                Save priority
              </button>
            )}
            {completed && (
              <button
                className="primary"
                disabled={changes.busy}
                onClick={() =>
                  changes.run(() =>
                    reopenCommitment(summary.commitmentId, data.revision),
                  )
                }
              >
                Reopen commitment
              </button>
            )}
            {canPause && (
              <button
                disabled={changes.busy}
                onClick={() => changes.run(() => lifecycleChange("paused"))}
              >
                Pause commitment
              </button>
            )}
            {canResume && (
              <button
                className="primary"
                disabled={changes.busy}
                onClick={() => changes.run(() => lifecycleChange("active"))}
              >
                Resume commitment
              </button>
            )}
            {canDismiss && (
              <button
                className="danger"
                disabled={changes.busy}
                onClick={() => changes.run(() => lifecycleChange("dismissed"))}
              >
                Dismiss commitment
              </button>
            )}
          </div>
          {changes.error ? <div className="error-note">{changes.error}</div> : null}
        </Card>
      )}

      <Card
        title="Source evidence"
        note="Minimal excerpts with provenance — message bodies are never stored."
      >
        {data.evidence.length === 0 && <Empty>No evidence excerpts recorded.</Empty>}
        {data.evidence.map((item, index) => (
          <blockquote className="evidence" key={index}>
            “{item.excerpt}”
            {item.confidence !== null && ` — confidence ${Math.round(item.confidence * 100)}%`}
          </blockquote>
        ))}
      </Card>

      <Card title="Work blocks">
        <div className="row-list">
          {data.workBlocks.length === 0 && <Empty>No blocks planned yet.</Empty>}
          {data.workBlocks.map((block) => (
            <CheckInRow key={block.workBlockId} block={block} refresh={refresh} />
          ))}
        </div>
      </Card>

      {!DEMO_MODE && canComplete && (
        <Card
          title="Complete this commitment"
          note="Completion is explicit and terminal: verified minutes are kept as-is, never fabricated to match the estimate."
        >
          <div className="inline-form">
            <input
              type="text"
              placeholder="Completion note (optional)"
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
            <button
              className="primary"
              disabled={completion.busy}
              onClick={() =>
                completion.run(() =>
                  completeCommitment(summary.commitmentId, data.revision, note),
                )
              }
            >
              Mark completed
            </button>
          </div>
          {completion.error ? <div className="error-note">{completion.error}</div> : null}
        </Card>
      )}

      {data.activity.length > 0 && (
        <Card title="Related activity">
          <div className="timeline">
            {data.activity.map((event, index) => (
              <div className="timeline-item" key={index}>
                <span className="when">{formatDateTime(event.createdAt)}</span>
                <div className="what">
                  <div className="type">{event.eventType.replaceAll("_", " ")}</div>
                  <div className="summary">{event.summary}</div>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </>
  );
}
