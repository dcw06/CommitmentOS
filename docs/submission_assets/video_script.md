# Demo video — final consolidated script and production plan

Target runtime **3:55** (confirm the actual rule limit before recording — the
"~4 minutes" figure is from our plan and reviews, not verified rule text).
Also confirm: required hosting (YouTube public/unlisted?), captions.

Audience: judges with zero prior context. The one thing this video must do
that nothing else can: **prove the real Gmail → Google Cloud → Calendar path
executes**, in a continuous unedited span, with Google Cloud visible.

---

## Timeline

| Time | Beat | Surface |
| --- | --- | --- |
| 0:00–0:25 | Hook + what it is | You / title card |
| 0:25–2:50 | **THE CONTINUOUS TAKE** (order below) | Gmail, dashboard, Calendar |
| 2:50–2:55 | Cloud Logging cutaway (timestamps) | Cloud Logging |
| 2:55–3:20 | Sandbox: live Gemini on a novel message | /sandbox (pre-staged) |
| 3:20–3:45 | Five-node architecture + the numbers | Diagram + Cloud Run console |
| 3:45–3:55 | Closing line + links | Title card |

Inside the continuous take, in this order (the order is load-bearing — see
"why" notes):

1. Send the request email (counterparty account, clean compose popup)
2. Candidate appears on dashboard with its evidence quote
3. Accept by reply → same record converges to my commitment
4. Confirm effort (type minutes) → planner runs → approve first plan
5. Google Calendar: the real blocks
6. **Completion beat on the pre-staged commitment** (check-in 60m → remaining
   drops exactly → mark complete → future block vanishes from Calendar)
7. Expand the completion audit entry and talk ~20s (NO calendar writes here —
   this is the quiet window that protects the next step from push throttling)
8. **Drag a meeting onto a reserved block → autonomous repair** (~9s)
9. Expand the plan_repaired audit entry: planner run, moved/preserved blocks,
   stable event ids, and the system's own measured `repair_latency_ms`

---

## Script

### 0:00–0:25 — Hook + what it is
*(You on camera or VO over your inbox, then title card)*

> "I promise things over email constantly. 'Sure — I'll get you the deck by
> Friday.' And then the promise just sits in my inbox. My calendar has no
> idea it exists. Nothing reserves the hours to actually do the work.
>
> So I built CommitmentOS: an AI agent that finds the promises in my email,
> books the work time on my Google Calendar, and repairs the plan by itself
> when reality changes. Everything you're about to see is the real deployed
> system on Google Cloud — one continuous take, no cuts."

### 0:25–2:50 — THE CONTINUOUS TAKE
*(One screen recording. Caption overlay spanning exactly the continuous
frames: "one continuous take — real accounts, deployed service". Address bar
visible. 110–125% browser zoom.)*

**1. Send the request** *(counterparty Gmail, clean compose popup — never
show the inbox list)*
> "A colleague asks me for something." *(type and send: "Hi — could you put
> together the Q4 budget summary? We need it by [day]." — pick the day per
> the date-math note below)* "Watch the deadline — 'by [day]' — that's just
> human language."

**2. Candidate appears** *(dashboard tab; expect 10–30s: push +
interpretation + 8s poll — narrate through it)*
> "That message is being pushed to the agent right now… there. And notice:
> it is NOT my commitment yet. It's a request, held as a candidate, with the
> exact sentence it's based on. The agent won't invent obligations for me."

**3. Accept by reply** *(my Gmail: "Yes, I'll take that on — you'll have it
by [day] end of day." Back to dashboard.)*
> "My 'yes' lands on the **same** record — it converges instead of creating
> a duplicate. One promise, one commitment, now mine."

**4. Effort + plan approval** *(dashboard; effort field is blank — that's
the point; type the minutes; brief planner wait before the plan card)*
> "It won't guess how long my work takes — I tell it: three hours. …It's
> computing a plan against my real calendar right now… and here's the second
> and last question it asks: may I write to your calendar? After this,
> in-policy repairs happen on their own." *(approve)*

**5. Real Calendar** *(Calendar tab)*
> "The blocks land here — real events, before the deadline, around
> everything already on my calendar."

**6. Completion beat** *(dashboard → the PRE-STAGED commitment)*
> "Here's a commitment from earlier this week — one of its work blocks
> elapsed this morning. The agent did NOT assume the work happened. It's
> asking me. I log the 60 minutes I actually did… remaining work drops by
> exactly 60. And when I mark it done — it keeps my real number, and cancels
> the reserved time I no longer need." *(Calendar tab: the future block is
> gone)* "Time passing never counts as progress. Only I do."

**7. Quiet-window talk** *(expand the completion audit entry; ~20s, no
calendar writes)*
> "Every one of these decisions lands on an audit timeline — what it saw,
> what it decided, what it wrote, all correlated."

**8. THE CLIMAX — conflict → repair** *(Calendar tab: drag a new meeting
directly onto a reserved block; hands off; count through it)*
> "Now the part I built this for. Someone books a meeting right on top of my
> reserved time. I'm not going to touch anything… *(wait; ~9s warmed)*
> …there. The agent noticed through the calendar's own change feed, moved
> exactly that one block, and left the others untouched. Nobody clicked
> 'replan.' Nobody even opened an app."

**9. The receipt** *(dashboard Activity: expand plan_repaired)*
> "And it can prove it: the planner run, the one moved block, the preserved
> ones, the stable event identity — and the repair latency the system
> measured on itself: [read the real number] seconds."

*(If the pause switch comes up at all, it is narration ONLY — do not toggle
it: "And if I ever want it to stop — one switch pauses automatic action;
anything in flight is held, not lost, and revalidated before it resumes.")*

### 2:50–2:55 — Cloud Logging cutaway *(separate 5s clip, pre-filtered query)*
> "Same story from Google Cloud's side — the webhook, the reconciliation,
> the calendar write, seconds apart."

### 2:55–3:20 — Sandbox: prove the AI is live
*(Separate take. Session PRE-STAGED: free-play lane already chosen, subject
already entered. On camera: type only the message, then expand the
interpretation evidence.)*

> "Skeptical the AI is real? This public sandbox runs the same brain on a
> test world — no login, link below. Here's a message I just made up…"
> *(type as counterparty: "Could you send me the revised onboarding guide by
> next Wednesday at 3pm?")* "…interpreted live by Gemini: there's the model,
> the wall-clock latency, and the exact words it cited."

*(If the label shows `recorded-fallback`: transient provider hiccup — wait
ten minutes or reset, then re-record this segment.)*

### 3:20–3:45 — Architecture + numbers
*(Five-node diagram `architecture_five_node.png`, then 2s of the Cloud Run
console showing the service + revision)*

> "Under the hood, one loop: Observe, Interpret, Plan, Policy, Execute — and
> every result is observed again, so the loop closes. Gemini only ever
> interprets language. Everything that touches my calendar is deterministic
> code — idempotent tasks, transactional outbox, etag preconditions so it
> can never overwrite a newer change.
>
> The receipts: ten consecutive end-to-end runs against the live deployment,
> and ten more after a hardening pass — 1,110 live acceptance checkpoints,
> zero duplicate commitments or events, 73 of 73 security probes, at about a
> twelfth of a cent per email."

### 3:45–3:55 — Close *(title card, both links + repo)*
> "CommitmentOS doesn't plan once. It keeps the promise feasible as reality
> changes."

---

## Pre-production (day before)

1. **Clean up, THEN stage** — run the audited controlled-account cleanup
   first; then create the pre-staged commitment (~120 min effort: one block
   that elapses the morning of recording + one later-in-week block that will
   visibly vanish on completion). Staging after cleanup, never before.
2. **Warm the instance**: `--min-instances 1` (verify actual current state;
   revert after recording).
3. **Verify**: monitoring ACTIVE and actions enabled (a paused monitor once
   silently killed a whole campaign); Gmail/Calendar watches current
   (renewal job is daily); zero stale pending approvals on Today; Calendar
   UI in Pacific.
4. **Date math**: choose the deadline expression for your recording day so
   it lands days out ("by Friday" from a Monday–Wednesday; otherwise "by
   next [day]"). More days = more visible placement spread.
5. **Accounts**: sign into `/app` fresh (12h session TTL). Counterparty
   sends from a separate browser profile — do NOT touch its OAuth (a
   wrong-account reconnect once poisoned a full night of runs). Set friendly
   display names on both accounts; the real controlled address WILL be on
   camera — deliberate choice, not an accident.
6. **Tabs prepped**: counterparty compose popup · dashboard · Calendar ·
   Cloud Logging with the filter query already typed · Cloud Run console.
   Clean profile, notifications off, canonical host in the address bar
   (`commitmentos-2hscowvydq-uw.a.run.app`).
7. **Rehearse the full take twice, recorded at full quality** — a rehearsal
   where everything lands IS a usable final take. Keep every take.

## Rehearsal pass criteria

- Push-to-candidate under 30 s; conflict-to-repair under 15 s; full
  continuous take under 2:45.
- Two failures → use the **split fallback**: two continuous takes with the
  seam after beat 5 (Calendar view), each captioned honestly ("continuous —
  no cuts") — never cut inside beats 8–9.
- If a suppressed push ever stalls the repair: keep rolling — the
  once-a-minute safety reconciliation catches it within ~60–75 s; narrate
  through it.
- Verify during rehearsal: completing the staged commitment does NOT shift
  the new commitment's blocks (preserve-then-allocate should hold them).

## Recording notes

- 1080p minimum, browser at 110–125% zoom (embedded players compress),
  cursor visible.
- Live mic during the continuous take (authenticity through the waits);
  clean VO for intro/architecture/close is fine.
- No copyrighted music.
- Captions/subtitles recommended.

## Post-recording

- Confirm monitoring active + actions enabled (mandatory if anything was
  toggled).
- Revert `--min-instances` if desired.
- Upload (unlisted YouTube unless rules say otherwise), test playback on a
  cold browser, then put the link in Devpost and the LinkedIn post.

## Truthfulness guardrails (non-negotiable)

- The "no cuts" caption spans exactly the frames that are continuous.
- Never call `/demo` "live" — it is seeded; the sandbox and the continuous
  take are the live proof.
- Read the repair latency number that actually appears on screen.
- "Ten more after a hardening pass" is the honest phrasing for campaign 2
  (seeded mode) — the proof index carries the scope detail.
