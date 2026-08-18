// Client for the interactive judge sandbox (/sandbox).
//
// The session id is held in this module and sent as an explicit header, not
// a cookie, so nothing the browser sends automatically carries authority
// here. It is kept in sessionStorage so a refresh resumes the same world;
// the server drops worlds on idle expiry, which surfaces as a 409 and is
// handled by transparently opening a fresh session.

const SESSION_HEADER = "X-Sandbox-Session";
const STORAGE_KEY = "commitmentos.sandbox.session";

export interface SandboxCard {
  card_id: string;
  kind: string;
  persona: string | null;
  label: string;
  body: string | null;
  available: boolean;
  blocked_reason: string | null;
}

export interface SandboxMessage {
  card_id: string;
  persona: string;
  sender: string;
  body: string;
  note: string;
}

export interface SandboxCommitment {
  commitmentId: string;
  title: string;
  ownershipType: string;
  lifecycleStatus: string;
  revision: number;
  deadline: string | null;
  deadlineExpression: string | null;
  deadlineConfidence: number | null;
  proposedMinutes: number | null;
  confirmedMinutes: number | null;
  verifiedMinutes: number | null;
  remainingMinutes: number | null;
  riskLevel: string | null;
  evidence: { excerpt: string; kind: string | null }[];
}

export interface SandboxBlock {
  workBlockId: string;
  commitmentId: string;
  title: string;
  start: string;
  end: string;
  durationMinutes: number;
  executionState: string;
  verifiedMinutes: number;
  calendarEventId: string | null;
  planRevision: number | null;
}

export interface SandboxApproval {
  approvalId: string;
  requestType: string;
  commitmentId: string | null;
  revision: number;
  reason: string | null;
  proposedMinutes: number | null;
  options: string[];
}

export interface SandboxActivity {
  eventId: string;
  eventType: string;
  summary: string;
  createdAt: string;
}

export interface SandboxCalendarEvent {
  eventId: string;
  summary: string;
  start: string;
  end: string;
}

export interface SandboxState {
  sessionId: string;
  now: string;
  interpretationSource: string;
  thread: SandboxMessage[];
  cards: SandboxCard[];
  commitments: SandboxCommitment[];
  blocks: SandboxBlock[];
  approvals: SandboxApproval[];
  activity: SandboxActivity[];
  calendar: SandboxCalendarEvent[];
  outcome?: { cardId: string; headline: string; detail: string };
}

export class SandboxError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

let sessionId: string | null = sessionStorage.getItem(STORAGE_KEY);

function remember(state: SandboxState): SandboxState {
  sessionId = state.sessionId;
  sessionStorage.setItem(STORAGE_KEY, state.sessionId);
  return state;
}

async function detail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    return typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

async function call(
  path: string,
  method: "GET" | "POST",
  body?: Record<string, unknown>,
): Promise<SandboxState> {
  const response = await fetch(path, {
    method,
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(sessionId ? { [SESSION_HEADER]: sessionId } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    throw new SandboxError(response.status, await detail(response));
  }
  return remember((await response.json()) as SandboxState);
}

export async function openSession(): Promise<SandboxState> {
  return call("/sandbox/api/session", "POST");
}

export async function resumeSession(): Promise<SandboxState> {
  if (!sessionId) return openSession();
  try {
    return await call("/sandbox/api/state", "GET");
  } catch (error) {
    // An expired world is the normal end of an idle session, not a failure.
    if (error instanceof SandboxError && error.status === 409) return openSession();
    throw error;
  }
}

export async function resetSession(): Promise<SandboxState> {
  sessionId = null;
  sessionStorage.removeItem(STORAGE_KEY);
  return openSession();
}

export async function playCard(cardId: string): Promise<SandboxState> {
  return call(`/sandbox/api/cards/${encodeURIComponent(cardId)}`, "POST", {});
}

export async function resolveSandboxApproval(
  approvalId: string,
  decision: "approve" | "reject",
  confirmedMinutes?: number,
): Promise<SandboxState> {
  return call(`/sandbox/api/approvals/${encodeURIComponent(approvalId)}`, "POST", {
    decision,
    ...(confirmedMinutes === undefined ? {} : { confirmed_minutes: confirmedMinutes }),
  });
}

export async function completeSandboxCommitment(
  commitmentId: string,
): Promise<SandboxState> {
  return call(
    `/sandbox/api/commitments/${encodeURIComponent(commitmentId)}/complete`,
    "POST",
    {},
  );
}

// The scenario is authored in Pacific time; render it that way regardless of
// where the judge is, so the story's "Monday morning" reads as intended.
const SCENARIO_ZONE = "America/Los_Angeles";

export function scenarioTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    timeZone: SCENARIO_ZONE,
    hour: "numeric",
    minute: "2-digit",
  });
}

export function scenarioDay(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", {
    timeZone: SCENARIO_ZONE,
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

export function scenarioHour(iso: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: SCENARIO_ZONE,
    hour: "numeric",
    minute: "numeric",
    hour12: false,
  }).formatToParts(new Date(iso));
  const hour = Number(parts.find((part) => part.type === "hour")?.value ?? "0");
  const minute = Number(parts.find((part) => part.type === "minute")?.value ?? "0");
  return hour + minute / 60;
}
