// The interactive judge sandbox: drive both sides of a thread and watch the
// agent work.
//
// Left is the simulated inbox — you play Jordan and yourself, plus the world
// events (a meeting lands, time passes). Right is the agent: what it
// understood, what it reserved, and what it did about the change. Every
// number on the right came out of the production stack running over an
// isolated in-memory twin; this view only renders what came back.

import { useCallback, useEffect, useState } from "react";

import {
  SandboxApproval,
  SandboxApprovalDecision,
  SandboxBlock,
  SandboxCard,
  SandboxCommitment,
  SandboxError,
  SandboxOwnershipType,
  SandboxState,
  chooseSandboxMode,
  completeSandboxCommitment,
  playCard,
  resetSession,
  retrySandboxMessage,
  resolveSandboxApproval,
  resumeSession,
  sendSandboxMessage,
  scenarioDay,
  scenarioHour,
  scenarioLocalDateTimeToIso,
  scenarioTime,
} from "../sandboxApi";
import { Badge, minutesLabel } from "../ui";

const APPROVAL_COPY: Record<string, { title: string; body: string }> = {
  effort_confirmation: {
    title: "How long will this take?",
    body:
      "The agent will not invent an estimate. It proposes what the thread " +
      "implies and waits for you to confirm the number it plans against.",
  },
  initial_plan_approval: {
    title: "Approve the first plan",
    body:
      "The first time the agent writes to your calendar, it asks. After " +
      "this, in-policy repairs happen on their own.",
  },
  action_approval: {
    title: "This change needs your decision",
    body:
      "The repair fell outside the autonomy policy, so the agent stopped and " +
      "escalated rather than making a large move on your behalf.",
  },
  identity_confirmation: {
    title: "Confirm how this message relates",
    body:
      "The identity resolver could not safely connect this message to one " +
      "existing commitment, so no revision was applied automatically.",
  },
  deadline_change_confirmation: {
    title: "Accept this proposed deadline?",
    body:
      "Someone else suggested a change to your commitment. The current " +
      "deadline stays binding unless you explicitly accept the proposal.",
  },
  deadline_required_confirmation: {
    title: "When is this due?",
    body:
      "The message creates a commitment but does not contain a deadline. " +
      "Choose one before the agent creates or schedules anything.",
  },
  retraction_confirmation: {
    title: "Cancel the existing commitment?",
    body:
      "The message appears to retract an earlier promise. The agent will not " +
      "close it until you explicitly confirm that intent.",
  },
};

const APPROVAL_REASON_COPY: Record<string, string> = {
  effort_rejected_edit_and_confirm:
    "The previous estimate was rejected. Edit it and confirm again.",
  revise_effort_or_request_another_plan:
    "The first plan was rejected. Revise the effort or request another plan.",
  portfolio_capacity_conflict:
    "Available capacity cannot cover the confirmed effort before the deadline.",
};

// ---------------------------------------------------------------------------
// Guided tour: what to do next, and why it matters
// ---------------------------------------------------------------------------

interface TourStep {
  step: number;
  title: string;
  body: string;
}

function tourStep(state: SandboxState): TourStep {
  if (state.mode === "unselected") {
    return {
      step: 1,
      title: "Choose a sandbox lane",
      body:
        "Run the authored end-to-end story, or open a separate free-play " +
        "thread for your own subject and messages. The two lanes never share " +
        "state or chronology.",
    };
  }
  if (state.mode === "free_play") {
    return {
      step: Math.min(state.thread.length + 1, 9),
      title: state.thread.length ? "Keep experimenting" : "Write the first message",
      body:
        "Every message stays in this visible subject thread and runs through " +
        "live interpretation plus the deterministic commitment workflow. " +
        "Start over when you want the guided repair story instead.",
    };
  }
  const played = new Set(state.thread.map((message) => message.card_id));
  const approval = state.approvals[0];
  const awaiting = state.blocks.some((block) => block.executionState === "awaiting_check_in");
  const planned = state.blocks.some((block) => block.executionState === "planned");
  const needsExplicitClosure = state.commitments.some(
    (commitment) =>
      !["completed", "dismissed", "canceled"].includes(
        commitment.lifecycleStatus,
      ) && commitment.remainingMinutes === 0,
  );
  const conflictAvailable = state.cards.some(
    (card) => card.card_id === "event_conflict" && card.available,
  );

  if (!played.has("msg_request")) {
    return {
      step: 1,
      title: "Send Jordan's request",
      body:
        "You are about to play both sides of an email thread. Start with the " +
        "message asking you for a vendor comparison, and watch what the agent " +
        "makes of it.",
    };
  }
  if (!played.has("msg_accept")) {
    return {
      step: 2,
      title: "Now accept it",
      body:
        "Right now this is someone else's request, not your commitment — the " +
        "agent is holding it as a candidate. Say yes and watch it converge " +
        "onto the same record instead of creating a second one.",
    };
  }
  if (approval?.requestType === "effort_confirmation") {
    return {
      step: 3,
      title: "Confirm the effort",
      body:
        "The agent read a deadline out of the thread, but it will not guess " +
        "how long the work takes. Confirm the estimate and it will plan " +
        "against exactly that number.",
    };
  }
  if (approval?.requestType === "initial_plan_approval") {
    return {
      step: 4,
      title: "Approve the plan",
      body:
        "It found time before the deadline, around everything already on the " +
        "calendar. Approve it and those blocks become real calendar events.",
    };
  }
  if (approval?.requestType === "deadline_change_confirmation") {
    return {
      step: 5,
      title: "Accept or reject Jordan's proposal",
      body:
        "Thursday is only a proposed deadline. The agent kept Friday " +
        "binding until you explicitly accept the change.",
    };
  }
  if (approval?.requestType === "identity_confirmation") {
    return {
      step: 5,
      title: "It stopped at the identity boundary",
      body:
        "The message could not be connected to the existing commitment with " +
        "enough certainty, so the agent made no silent revision.",
    };
  }
  if (approval) {
    return {
      step: 5,
      title: "It stopped and asked you",
      body:
        "This change was too large to make on its own, so it escalated " +
        "instead of quietly rearranging your week. That boundary is the " +
        "product.",
    };
  }
  if (!played.has("msg_deadline_change")) {
    return {
      step: 5,
      title: "Propose an earlier deadline",
      body:
        "Jordan asks to move the review up by a day. Send the update and " +
        "watch the agent hold it for your acceptance instead of silently " +
        "changing your commitment.",
    };
  }
  if (conflictAvailable) {
    return {
      step: 6,
      title: "Now break the plan",
      body:
        "Drop a meeting on top of a block the agent reserved. Nobody tells it " +
        "to fix anything — it notices through the calendar change feed and " +
        "repairs the plan itself.",
    };
  }
  if (planned && !awaiting && state.cards.some((card) => card.kind === "advance" && card.available)) {
    return {
      step: 7,
      title: "Let time pass",
      body:
        "Fast-forward past a reserved block. The agent will not assume the " +
        "work happened just because the calendar says it should have.",
    };
  }
  if (awaiting) {
    return {
      step: 8,
      title: "Tell it what actually happened",
      body:
        "Progress is only ever what you confirm. Log your verified minutes " +
        "and watch the remaining work fall by exactly that much.",
    };
  }
  if (needsExplicitClosure) {
    return {
      step: 9,
      title: "Close the commitment explicitly",
      body:
        "All confirmed effort is now verified. Use Mark this done to close " +
        "the commitment with explicit completion evidence.",
    };
  }
  if (state.cards.some((card) => card.available)) {
    return {
      step: 8,
      title: "Keep going",
      body:
        "Send the remaining message or change the world again — the agent " +
        "reacts to each one the same way it would to the real thing.",
    };
  }
  return {
    step: 9,
    title: "That is the whole loop",
    body:
      "Extraction from real language, an explicit confirmation boundary, a " +
      "plan on your calendar, an autonomous repair when reality moved, and " +
      "honest progress. Reset to run it again, or open the dashboard to see " +
      "the same system on seeded demonstration data.",
  };
}

function ModeChooser({
  busy,
  onChoose,
}: {
  busy: boolean;
  onChoose: (
    mode: "guided" | "free_play",
    subject?: string,
  ) => Promise<boolean>;
}) {
  const [subject, setSubject] = useState("");
  const trimmed = subject.trim();
  return (
    <div className="sandbox-mode-chooser">
      <div>
        <h3>Guided end-to-end story</h3>
        <p>Use the authored thread, approvals, calendar conflict, repair, and check-in.</p>
        <button
          type="button"
          disabled={busy}
          onClick={() => void onChoose("guided")}
        >
          Start guided story
        </button>
      </div>
      <div>
        <h3>Free-play email thread</h3>
        <p>Choose a visible subject, then write as Jordan or yourself.</p>
        <label>
          Subject
          <input
            value={subject}
            maxLength={200}
            placeholder="e.g. Revised hiring plan"
            disabled={busy}
            onChange={(event) => setSubject(event.target.value)}
          />
        </label>
        <button
          type="button"
          disabled={busy || !trimmed}
          onClick={() => void onChoose("free_play", trimmed)}
        >
          Start free play
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Panels
// ---------------------------------------------------------------------------

function CardButton({
  card,
  busy,
  onPlay,
}: {
  card: SandboxCard;
  busy: boolean;
  onPlay: (cardId: string) => void;
}) {
  const isMessage = card.kind === "message";
  return (
    <div className={`sandbox-card ${card.available ? "is-available" : "is-blocked"}`}>
      <div className="sandbox-card-head">
        {isMessage ? (
          <span className={`sandbox-persona persona-${card.persona}`}>
            {card.persona === "jordan" ? "Jordan" : "You"}
          </span>
        ) : (
          <span className="sandbox-persona persona-world">World</span>
        )}
        <span className="sandbox-card-label">{card.label}</span>
      </div>
      {card.body ? <p className="sandbox-card-body">“{card.body}”</p> : null}
      <button
        type="button"
        disabled={!card.available || busy}
        onClick={() => onPlay(card.card_id)}
      >
        {isMessage ? "Send this message" : "Make it happen"}
      </button>
      {!card.available && card.blocked_reason ? (
        <p className="sandbox-blocked">{card.blocked_reason}</p>
      ) : null}
    </div>
  );
}

function FreePlayComposer({
  busy,
  remaining,
  onSend,
}: {
  busy: boolean;
  remaining: number;
  onSend: (sender: "you" | "jordan", message: string) => Promise<boolean>;
}) {
  const [sender, setSender] = useState<"you" | "jordan">("jordan");
  const [message, setMessage] = useState("");
  const trimmed = message.trim();

  return (
    <form
      className="sandbox-composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (!trimmed || busy || remaining === 0) return;
        void onSend(sender, trimmed).then((sent) => {
          if (sent) setMessage("");
        });
      }}
    >
      <div className="sandbox-composer-head">
        <div>
          <h3>Try your own message</h3>
          <p>
            Choose either participant and send any text. You can send several
            messages in a row from the same person; all stay in this thread.
          </p>
        </div>
        <label>
          Send as
          <select
            value={sender}
            disabled={busy || remaining === 0}
            onChange={(event) =>
              setSender(event.target.value as "you" | "jordan")
            }
          >
            <option value="jordan">Jordan Ellis</option>
            <option value="you">You</option>
          </select>
        </label>
      </div>
      <label className="sandbox-message-field">
        Message
        <textarea
          value={message}
          maxLength={1000}
          rows={4}
          placeholder={
            sender === "jordan"
              ? "Could you send me the revised brief by Tuesday?"
              : "Yes — I’ll send it by Tuesday afternoon."
          }
          disabled={busy || remaining === 0}
          onChange={(event) => setMessage(event.target.value)}
        />
      </label>
      <div className="sandbox-composer-actions">
        <span>
          {message.length}/1000 · {remaining} custom message{remaining === 1 ? "" : "s"} remaining
        </span>
        <button type="submit" disabled={busy || !trimmed || remaining === 0}>
          {busy ? "Working…" : `Send as ${sender === "jordan" ? "Jordan" : "You"}`}
        </button>
      </div>
    </form>
  );
}

export function ApprovalPanel({
  approval,
  busy,
  onResolve,
}: {
  approval: SandboxApproval;
  busy: boolean;
  onResolve: (
    approvalId: string,
    decision: SandboxApprovalDecision,
  ) => void;
}) {
  const isDeadlineProposal =
    approval.requestType === "deadline_change_confirmation";
  const isPlanReconsideration =
    approval.reason === "revise_effort_or_request_another_plan";
  const copy = isDeadlineProposal
    ? {
        title: "Accept this proposed deadline?",
        body:
          "Someone else suggested a change to your commitment. Friday stays " +
          "binding unless you explicitly accept Thursday.",
      }
    : isPlanReconsideration
      ? {
          title: "Revise the estimate or request another plan",
          body:
            "The first plan was rejected. Edit the effort, or confirm the same " +
            "estimate to calculate another plan from the current calendar.",
        }
    : APPROVAL_COPY[approval.requestType] ?? {
        title: approval.requestType.replaceAll("_", " "),
        body: "The agent needs a decision before it acts.",
      };
  const needsMinutes = approval.requestType === "effort_confirmation";
  const needsDeadline =
    approval.requestType === "deadline_required_confirmation";
  const [minutes, setMinutes] = useState<number | "">(
    approval.proposedMinutes ?? "",
  );
  const [ownership, setOwnership] = useState<SandboxOwnershipType>(
    approval.ownershipOptions[0] ?? "my_commitment",
  );
  const [choice, setChoice] = useState(approval.options[0] ?? "");
  const [rejectionReason, setRejectionReason] = useState("");
  const [deadline, setDeadline] = useState("");
  const validMinutes =
    typeof minutes === "number" &&
    minutes >= 30 &&
    minutes <= 2400 &&
    minutes % 15 === 0;
  const normalizedDeadline = scenarioLocalDateTimeToIso(deadline);
  const validDeadline = normalizedDeadline !== null;
  const commitmentLabel = approval.commitmentTitle ?? "this commitment";
  const canApprove =
    (!needsMinutes || validMinutes) &&
    (!needsDeadline || validDeadline) &&
    (!approval.requiresOwnership || Boolean(ownership)) &&
    (approval.options.length === 0 || Boolean(choice));

  const approve = () => {
    const decision: SandboxApprovalDecision = { decision: "approve" };
    if (needsMinutes && typeof minutes === "number") {
      decision.confirmed_minutes = minutes;
    }
    if (needsDeadline && normalizedDeadline) {
      decision.deadline = normalizedDeadline;
    }
    if (approval.requiresOwnership && ownership) {
      decision.ownership_type = ownership;
    }
    if (choice) decision.choice = choice;
    onResolve(approval.approvalId, decision);
  };

  const reject = () => {
    onResolve(approval.approvalId, {
      decision: "reject",
      ...(rejectionReason.trim() ? { reason: rejectionReason.trim() } : {}),
    });
  };

  return (
    <div className="sandbox-approval">
      <h3>{copy.title}</h3>
      <p>{copy.body}</p>
      {approval.commitmentTitle ? (
        <p className="sandbox-approval-context">
          Commitment: <strong>{approval.commitmentTitle}</strong>
        </p>
      ) : null}
      {approval.normalizedOutcome ? (
        <p className="sandbox-approval-context">
          Proposed outcome: <strong>{approval.normalizedOutcome}</strong>
        </p>
      ) : null}
      {approval.proposedDeadline ? (
        <p className="sandbox-approval-context">
          Proposed deadline: <strong>{scenarioDay(approval.proposedDeadline)}</strong>
          {approval.proposedDeadlineExpression
            ? ` from “${approval.proposedDeadlineExpression}”`
            : ""}
        </p>
      ) : null}
      {approval.reason ? (
        <p className="sandbox-approval-context">
          Why it stopped: {APPROVAL_REASON_COPY[approval.reason] ?? approval.reason.replaceAll("_", " ")}
        </p>
      ) : null}
      {approval.previousRejectionReason ? (
        <p className="sandbox-approval-context">
          Your previous rejection reason: {approval.previousRejectionReason}
        </p>
      ) : null}
      {needsMinutes ? (
        <label>
          Minutes for {commitmentLabel}
          <input
            type="number"
            min={30}
            max={2400}
            step={15}
            value={minutes}
            placeholder="Enter minutes"
            onChange={(event) =>
              setMinutes(event.target.value ? Number(event.target.value) : "")
            }
          />
        </label>
      ) : null}
      {needsDeadline ? (
        <label>
          Deadline for {commitmentLabel}
          <input
            type="datetime-local"
            required
            value={deadline}
            disabled={busy}
            onChange={(event) => setDeadline(event.target.value)}
          />
        </label>
      ) : null}
      {approval.requiresOwnership ? (
        <label>
          How does this message relate?
          <select
            value={ownership}
            disabled={busy}
            onChange={(event) =>
              setOwnership(
                event.target.value as SandboxOwnershipType,
              )
            }
          >
            {approval.ownershipOptions.map((option) => (
              <option key={option} value={option}>
                {option === "my_commitment"
                  ? "I am committing to do this"
                  : option === "request_to_me"
                    ? "Someone is asking me"
                    : "Someone else committed to me"}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {approval.options.length > 0 ? (
        <label>
          Choose what happens next
          <select
            value={choice}
            disabled={busy}
            onChange={(event) => setChoice(event.target.value)}
          >
            {approval.options.map((option) => (
              <option key={option} value={option}>
                {option.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <label>
        Rejection reason for {commitmentLabel} (optional)
        <input
          type="text"
          maxLength={500}
          value={rejectionReason}
          disabled={busy}
          placeholder="Why should the agent dismiss this?"
          aria-label={`Rejection reason for ${commitmentLabel}`}
          onChange={(event) => setRejectionReason(event.target.value)}
        />
      </label>
      <div className="sandbox-approval-actions">
        <button
          type="button"
          className="secondary"
          disabled={busy}
          aria-label={`Reject ${commitmentLabel}`}
          onClick={reject}
        >
          Reject
        </button>
        <button type="button" disabled={busy || !canApprove} onClick={approve}>
          {isDeadlineProposal
            ? "Accept deadline"
            : needsDeadline
              ? "Confirm deadline"
            : needsMinutes && typeof minutes === "number"
            ? `Confirm ${minutesLabel(minutes)}`
            : needsMinutes
              ? "Enter the effort"
              : "Approve"}
        </button>
      </div>
    </div>
  );
}

const DAY_START = 8;
const DAY_END = 19;

function CalendarPanel({ state }: { state: SandboxState }) {
  const entries = [
    ...state.blocks.map((block) => ({
      key: block.workBlockId,
      kind: `block state-${block.executionState}`,
      label: block.title,
      sub: block.executionState.replaceAll("_", " "),
      start: block.start,
      end: block.end,
    })),
    ...state.calendar.map((event) => ({
      key: event.eventId,
      kind: "busy",
      label: event.summary,
      sub: "not yours",
      start: event.start,
      end: event.end,
    })),
  ];
  if (entries.length === 0) {
    return (
      <div className="sandbox-empty">
        Nothing is reserved yet. Once you approve a plan, the blocks the agent
        chose appear here.
      </div>
    );
  }

  const days = [...new Set(entries.map((entry) => scenarioDay(entry.start)))].sort(
    (left, right) =>
      new Date(entries.find((entry) => scenarioDay(entry.start) === left)!.start).getTime() -
      new Date(entries.find((entry) => scenarioDay(entry.start) === right)!.start).getTime(),
  );

  return (
    <div className="sandbox-calendar" style={{ gridTemplateColumns: `3rem repeat(${days.length}, 1fr)` }}>
      <div className="calendar-corner" />
      {days.map((day) => (
        <div key={day} className="calendar-day-head">
          {day}
        </div>
      ))}
      <div className="calendar-hours">
        {Array.from({ length: DAY_END - DAY_START }, (_, index) => (
          <span key={index} style={{ top: `${(index / (DAY_END - DAY_START)) * 100}%` }}>
            {DAY_START + index}
          </span>
        ))}
      </div>
      {days.map((day) => (
        <div key={day} className="calendar-column">
          {entries
            .filter((entry) => scenarioDay(entry.start) === day)
            .map((entry) => {
              const top = ((scenarioHour(entry.start) - DAY_START) / (DAY_END - DAY_START)) * 100;
              const height =
                ((scenarioHour(entry.end) - scenarioHour(entry.start)) / (DAY_END - DAY_START)) * 100;
              return (
                <div
                  key={entry.key}
                  className={`calendar-entry ${entry.kind}`}
                  style={{ top: `${top}%`, height: `${Math.max(height, 6)}%` }}
                  title={`${entry.label} · ${scenarioTime(entry.start)}`}
                >
                  <strong>{entry.label}</strong>
                  <span>
                    {scenarioTime(entry.start)} · {entry.sub}
                  </span>
                </div>
              );
            })}
        </div>
      ))}
    </div>
  );
}

function blockSummary(blocks: SandboxBlock[]): string {
  if (blocks.length === 0) return "no reserved time yet";
  const reserved = blocks.filter((block) =>
    ["planned", "active"].includes(block.executionState),
  );
  const completed = blocks.filter(
    (block) => block.executionState === "completed",
  );
  const canceled = blocks.filter(
    (block) => block.executionState === "canceled",
  );
  const awaiting = blocks.filter(
    (block) => block.executionState === "awaiting_check_in",
  );
  const parts = [
    `${reserved.length} reserved block${reserved.length === 1 ? "" : "s"}`,
    `${minutesLabel(reserved.reduce((sum, block) => sum + block.durationMinutes, 0))} reserved`,
  ];
  if (completed.length > 0) {
    parts.push(
      `${minutesLabel(completed.reduce((sum, block) => sum + block.verifiedMinutes, 0))} completed`,
    );
  }
  if (canceled.length > 0) {
    parts.push(
      `${canceled.length} future block${canceled.length === 1 ? "" : "s"} canceled`,
    );
  }
  if (awaiting.length > 0) {
    parts.push(`${awaiting.length} awaiting check-in`);
  }
  return parts.join(" · ");
}

function interpretationLabel(source: string): string {
  if (source === "not-run") return "not run yet";
  return source.replaceAll("-", " ");
}

function CommitmentPanel({
  commitment,
  blocks,
  busy,
  onComplete,
}: {
  commitment: SandboxCommitment;
  blocks: SandboxBlock[];
  busy: boolean;
  onComplete: (commitmentId: string) => void;
}) {
  const terminal = ["completed", "dismissed", "canceled"].includes(
    commitment.lifecycleStatus,
  );
  const hasActiveBlocks = blocks.some(
    (block) =>
      block.commitmentId === commitment.commitmentId &&
      ["planned", "active", "awaiting_check_in"].includes(block.executionState),
  );
  const canComplete =
    !terminal &&
    (hasActiveBlocks ||
      (commitment.confirmedMinutes !== null &&
        commitment.remainingMinutes === 0));
  return (
    <div className="sandbox-commitment">
      <div className="sandbox-commitment-head">
        <h3>{commitment.title}</h3>
        <Badge value={commitment.ownershipType} />
        <Badge value={commitment.lifecycleStatus} />
        {commitment.riskLevel ? <Badge value={commitment.riskLevel} /> : null}
      </div>
      <dl>
        <div>
          <dt>Deadline</dt>
          <dd>
            {commitment.deadline ? scenarioDay(commitment.deadline) : "—"}
            {commitment.deadlineExpression ? (
              <em> from “{commitment.deadlineExpression}”</em>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Effort</dt>
          <dd>
            {commitment.confirmedMinutes
              ? `${minutesLabel(commitment.confirmedMinutes)} confirmed by you`
              : `${minutesLabel(commitment.proposedMinutes)} proposed, unconfirmed`}
          </dd>
        </div>
        <div>
          <dt>Verified</dt>
          <dd>
            {commitment.lifecycleStatus === "completed"
              ? `${minutesLabel(commitment.verifiedMinutes ?? 0)} verified at completion · no remaining work`
              : terminal
                ? `${minutesLabel(commitment.verifiedMinutes ?? 0)} verified · no active remaining work`
              : `${minutesLabel(commitment.verifiedMinutes ?? 0)} done · ${minutesLabel(commitment.remainingMinutes)} remaining`}
          </dd>
        </div>
        <div>
          <dt>Revision</dt>
          <dd>{commitment.revision}</dd>
        </div>
      </dl>
      {commitment.evidence.map((evidence, index) => (
        <p className="sandbox-evidence" key={`${evidence.createdAt ?? "evidence"}-${index}`}>
          {evidence.supportsDeadline
            ? evidence.kind === "deadline_confirmation"
              ? "Explicit deadline confirmation"
              : "Current deadline evidence"
            : evidence.kind === "commitment_completion"
              ? "Completion evidence"
              : "Earlier evidence"}
          : “{evidence.excerpt}”
        </p>
      ))}
      {canComplete ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => onComplete(commitment.commitmentId)}
        >
          Mark this done
        </button>
      ) : null}
      {commitment.lifecycleStatus === "completed" ? (
        <p className="sandbox-note">
          Completed with {minutesLabel(commitment.verifiedMinutes ?? 0)} verified —
          the agent never inflated that number to match the plan.
        </p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------

export function SandboxPage() {
  const [state, setState] = useState<SandboxState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    resumeSession()
      .then(setState)
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : "sandbox unavailable"),
      );
  }, []);

  const run = useCallback(async (action: () => Promise<SandboxState>) => {
    setBusy(true);
    setError(null);
    try {
      setState(await action());
      return true;
    } catch (cause) {
      if (
        cause instanceof SandboxError &&
        cause.status === 409 &&
        cause.message === "sandbox session expired"
      ) {
        // The world expired mid-story; start a clean one rather than
        // stranding the judge on a dead session.
        setState(await resetSession());
        setError("That sandbox expired, so this is a fresh one.");
      } else {
        setError(cause instanceof Error ? cause.message : "action failed");
      }
      return false;
    } finally {
      setBusy(false);
    }
  }, []);

  if (error && !state) return <div className="loading">{error}</div>;
  if (!state) return <div className="loading">Opening a sandbox…</div>;

  const tour = tourStep(state);
  const messageCards = state.cards.filter((card) => card.kind === "message");
  const worldCards = state.cards.filter((card) => card.kind !== "message");

  return (
    <div className="sandbox">
      <div className="sandbox-tour">
        <span className="sandbox-step">Step {tour.step}</span>
        <div>
          <h2>{tour.title}</h2>
          <p>{tour.body}</p>
        </div>
        <button type="button" onClick={() => void run(resetSession)} disabled={busy}>
          Start over
        </button>
      </div>

      {error ? <div className="sandbox-error">{error}</div> : null}
      {state.commitments.length > 1 ? (
        <div className="sandbox-error" role="alert">
          This story currently has {state.commitments.length} commitments. The
          divergence is shown rather than hidden.
        </div>
      ) : null}

      <div className="sandbox-split">
        <section className="sandbox-left">
          <h2>
            The thread
            {state.threadSubject ? (
              <span className="sandbox-thread-subject">{state.threadSubject}</span>
            ) : null}
          </h2>
          <p className="section-note">
            You are both people in this simulated mailbox. Free-play text is sent
            to the sandbox-only Gemini model and is not written to Firestore.
          </p>

          {state.mode === "unselected" ? (
            <ModeChooser
              busy={busy}
              onChoose={(mode, subject) =>
                run(() => chooseSandboxMode(mode, subject))
              }
            />
          ) : null}

          <div className="sandbox-thread">
            {state.thread.length === 0 ? (
              <div className="sandbox-empty">The thread starts when you send the first message.</div>
            ) : (
              state.thread.map((message) => (
                <div key={message.card_id} className={`sandbox-message persona-${message.persona}`}>
                  <span className="sandbox-sender">
                    {message.sender}
                    {message.custom ? <em>your experiment</em> : null}
                  </span>
                  <p>{message.body}</p>
                  <p className="sandbox-note">{message.note}</p>
                  {message.retryAvailable ? (
                    <div className="sandbox-retry">
                      <p>
                        The model output was rejected by deterministic validation.
                        You can retry this same message once.
                      </p>
                      <button
                        type="button"
                        className="secondary"
                        disabled={busy}
                        aria-label={`Retry interpretation for message from ${message.sender}`}
                        onClick={() =>
                          void run(() => retrySandboxMessage(message.card_id))
                        }
                      >
                        Retry interpretation
                      </button>
                    </div>
                  ) : null}
                </div>
              ))
            )}
          </div>

          {state.mode === "free_play" ? (
            <FreePlayComposer
              busy={busy}
              remaining={state.customMessagesRemaining}
              onSend={(sender, message) =>
                run(() => sendSandboxMessage(sender, message))
              }
            />
          ) : null}

          {state.mode === "guided" && messageCards.length > 0 ? (
            <>
              <h3>Follow the guided story</h3>
              {messageCards.map((card) => (
                <CardButton
                  key={card.card_id}
                  card={card}
                  busy={busy}
                  onPlay={(cardId) => void run(() => playCard(cardId))}
                />
              ))}
            </>
          ) : null}

          {state.mode === "guided" && messageCards.length === 0 && worldCards.length === 0 ? (
            <div className="sandbox-empty">
              You have played every card in the deck. Start over to run the story
              again.
            </div>
          ) : null}

          {state.mode !== "unselected" && worldCards.length > 0 ? (
            <>
              <h3>{state.mode === "guided" ? "Or change the world" : "Test the plan"}</h3>
              {worldCards.map((card) => (
                <CardButton
                  key={card.card_id}
                  card={card}
                  busy={busy}
                  onPlay={(cardId) => void run(() => playCard(cardId))}
                />
              ))}
            </>
          ) : null}
        </section>

        <section className="sandbox-right">
          <h2>The agent</h2>
          <p className="section-note">
            Scenario time {scenarioTime(state.now)} · {scenarioDay(state.now)} ·
            interpretation: {interpretationLabel(state.interpretationSource)}
          </p>

          {state.outcome ? (
            <div className="sandbox-outcome">
              <strong>{state.outcome.headline}</strong>
              <p>{state.outcome.detail}</p>
            </div>
          ) : null}

          {state.approvals.map((approval) => (
            <ApprovalPanel
              key={approval.approvalId}
              approval={approval}
              busy={busy}
              onResolve={(approvalId, decision) =>
                void run(() => resolveSandboxApproval(approvalId, decision))
              }
            />
          ))}

          {state.commitments.length > 0 ? (
            state.commitments.map((commitment) => (
              <CommitmentPanel
                key={commitment.commitmentId}
                commitment={commitment}
                blocks={state.blocks}
                busy={busy}
                onComplete={(commitmentId) =>
                  void run(() => completeSandboxCommitment(commitmentId))
                }
              />
            ))
          ) : (
            <div className="sandbox-empty">
              No commitment yet. Send the first message and the agent will read it.
            </div>
          )}

          <h3>Your calendar <span className="sandbox-subtle">{blockSummary(state.blocks)}</span></h3>
          <CalendarPanel state={state} />

          <h3>What the agent did</h3>
          <ol className="sandbox-activity">
            {state.activity
              .slice()
              .reverse()
              .slice(0, 12)
              .map((event) => (
                <li key={event.eventId}>
                  <code>{event.eventType}</code>
                  <span>{event.summary}</span>
                </li>
              ))}
          </ol>
        </section>
      </div>
    </div>
  );
}
