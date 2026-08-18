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
  SandboxBlock,
  SandboxCard,
  SandboxError,
  SandboxState,
  completeSandboxCommitment,
  playCard,
  resetSession,
  resolveSandboxApproval,
  resumeSession,
  scenarioDay,
  scenarioHour,
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
  const played = new Set(state.thread.map((message) => message.card_id));
  const approval = state.approvals[0];
  const awaiting = state.blocks.some((block) => block.executionState === "awaiting_check_in");
  const planned = state.blocks.some((block) => block.executionState === "planned");
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
  if (conflictAvailable) {
    return {
      step: 5,
      title: "Now break the plan",
      body:
        "Drop a meeting on top of a block the agent reserved. Nobody tells it " +
        "to fix anything — it notices through the calendar change feed and " +
        "repairs the plan itself.",
    };
  }
  if (planned && !awaiting && state.cards.some((card) => card.kind === "advance" && card.available)) {
    return {
      step: 6,
      title: "Let time pass",
      body:
        "Fast-forward past a reserved block. The agent will not assume the " +
        "work happened just because the calendar says it should have.",
    };
  }
  if (awaiting) {
    return {
      step: 7,
      title: "Tell it what actually happened",
      body:
        "Progress is only ever what you confirm. Log your verified minutes " +
        "and watch the remaining work fall by exactly that much.",
    };
  }
  if (state.cards.some((card) => card.available)) {
    return {
      step: 6,
      title: "Keep going",
      body:
        "Send the remaining message or change the world again — the agent " +
        "reacts to each one the same way it would to the real thing.",
    };
  }
  return {
    step: 8,
    title: "That is the whole loop",
    body:
      "Extraction from real language, an explicit confirmation boundary, a " +
      "plan on your calendar, an autonomous repair when reality moved, and " +
      "honest progress. Reset to run it again, or open the dashboard to see " +
      "the same system on live data.",
  };
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

function ApprovalPanel({
  approval,
  busy,
  onResolve,
}: {
  approval: SandboxApproval;
  busy: boolean;
  onResolve: (approvalId: string, minutes?: number) => void;
}) {
  const copy = APPROVAL_COPY[approval.requestType] ?? {
    title: approval.requestType.replaceAll("_", " "),
    body: "The agent needs a decision before it acts.",
  };
  const needsMinutes = approval.requestType === "effort_confirmation";
  const [minutes, setMinutes] = useState(approval.proposedMinutes ?? 180);

  return (
    <div className="sandbox-approval">
      <h3>{copy.title}</h3>
      <p>{copy.body}</p>
      {needsMinutes ? (
        <label>
          Minutes of work
          <input
            type="number"
            min={15}
            max={2400}
            step={15}
            value={minutes}
            onChange={(event) => setMinutes(Number(event.target.value))}
          />
        </label>
      ) : null}
      <button
        type="button"
        disabled={busy}
        onClick={() => onResolve(approval.approvalId, needsMinutes ? minutes : undefined)}
      >
        {needsMinutes ? `Confirm ${minutesLabel(minutes)}` : "Approve"}
      </button>
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
      label: "Vendor comparison",
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
  const total = blocks.reduce((sum, block) => sum + block.durationMinutes, 0);
  return `${blocks.length} blocks · ${minutesLabel(total)} reserved`;
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
    } catch (cause) {
      if (cause instanceof SandboxError && cause.status === 409) {
        // The world expired mid-story; start a clean one rather than
        // stranding the judge on a dead session.
        setState(await resetSession());
        setError("That sandbox expired, so this is a fresh one.");
      } else {
        setError(cause instanceof Error ? cause.message : "action failed");
      }
    } finally {
      setBusy(false);
    }
  }, []);

  if (error && !state) return <div className="loading">{error}</div>;
  if (!state) return <div className="loading">Opening a sandbox…</div>;

  const tour = tourStep(state);
  const commitment = state.commitments[0];
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

      <div className="sandbox-split">
        <section className="sandbox-left">
          <h2>The thread</h2>
          <p className="section-note">
            You are both people in this conversation. Nothing here touches a real
            mailbox.
          </p>

          <div className="sandbox-thread">
            {state.thread.length === 0 ? (
              <div className="sandbox-empty">The thread starts when you send the first message.</div>
            ) : (
              state.thread.map((message) => (
                <div key={message.card_id} className={`sandbox-message persona-${message.persona}`}>
                  <span className="sandbox-sender">{message.sender}</span>
                  <p>{message.body}</p>
                  <p className="sandbox-note">{message.note}</p>
                </div>
              ))
            )}
          </div>

          {messageCards.length > 0 ? (
            <>
              <h3>Send next</h3>
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

          {messageCards.length === 0 && worldCards.length === 0 ? (
            <div className="sandbox-empty">
              You have played every card in the deck. Start over to run the story
              again.
            </div>
          ) : null}

          {worldCards.length > 0 ? (
            <>
              <h3>Or change the world</h3>
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
            interpretation: {state.interpretationSource.replaceAll("-", " ")}
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
              onResolve={(approvalId, minutes) =>
                void run(() => resolveSandboxApproval(approvalId, "approve", minutes))
              }
            />
          ))}

          {commitment ? (
            <div className="sandbox-commitment">
              <div className="sandbox-commitment-head">
                <h3>{commitment.title}</h3>
                <Badge value={commitment.ownershipType} />
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
                    {minutesLabel(commitment.verifiedMinutes ?? 0)} done ·{" "}
                    {minutesLabel(commitment.remainingMinutes)} remaining
                  </dd>
                </div>
                <div>
                  <dt>Revision</dt>
                  <dd>{commitment.revision}</dd>
                </div>
              </dl>
              {commitment.evidence.length > 0 ? (
                <p className="sandbox-evidence">
                  Evidence: “{commitment.evidence[0].excerpt}”
                </p>
              ) : null}
              {commitment.lifecycleStatus !== "completed" && state.blocks.length > 0 ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void run(() => completeSandboxCommitment(commitment.commitmentId))}
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
