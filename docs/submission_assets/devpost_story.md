# CommitmentOS — Devpost story

## What it does

CommitmentOS doesn't just find the commitments you make in your email. It schedules your work, replans when your schedule changes, and verifies that your work is actually done.

An email can record what you promised and when it's due, but it doesn't automatically reserve a time slot on your calendar for you to get it done. CommitmentOS fills that gap: it detects your promise, reserves the time on your Google Calendar, adapts the schedule as things change, and verifies task completion.

Gemini 3.5 Flash reads your Gmail threads and identifies who promised what, to whom, and by when, including deadline changes negotiated in Gmail replies. For every commitment it detects, CommitmentOS points to the exact words in the email that support it; if it cannot find that evidence, the commitment is rejected.

Once a commitment is detected, CommitmentOS estimates the amount of work it will take. Nothing is scheduled on Google Calendar until you confirm that estimate, and the first Calendar plan requires your approval. A deterministic planner then turns the confirmed effort into Calendar blocks, ensuring that no two commitments are ever assigned to the same time slot.

Once the work is scheduled, CommitmentOS continues watching your Google Calendar. If it detects that a new meeting conflicts with your plan, it automatically moves the affected block — typically within 7–10 seconds — and shows you what changed, why it changed, and how to undo it. If you move a block yourself, CommitmentOS respects the change; if you delete one, it doesn't silently recreate it.

But scheduling time doesn't mean the work is actually completed. You check in the minutes you actually worked, building a verified record of real progress — but only you can mark a commitment complete. CommitmentOS never assumes the job is done just because time has passed or a block has elapsed. If a task estimated at 180 minutes actually took you 120 verified minutes, and you mark it complete, CommitmentOS closes it at 120. It does not treat the original estimate as the reality.

Every decision, change, and policy call is logged on an audit timeline, and automatic actions can be paused and safely resumed at any time. A read-only judge mode is live at `/demo` — no sign-in, no live data, no mutation capability.

In ten consecutive test runs against the deployed service, this loop completed successfully every time, with automatic repairs landing in about nine seconds on average.

## How we built it

The architecture rests on one rule: Gemini interprets, code decides. Gemini's only job is to read the ambiguous part of an email — who promised what, to whom, and by when. It does that inside one small, contained step, built with Google's Agent Development Kit (ADK). Everything that happens after that reading — matching the promise to an existing commitment, checking the schedule, applying policy, and writing to the calendar — is ordinary, deterministic code. The model never decides anything; it only reads.

That only works if the deterministic side never forgets what it decided. The whole backend is one Python service (FastAPI) running on Cloud Run, Google's serverless hosting, with Firestore — Google's managed database — as the single source of truth. Every fact the system pulls from an email, every revision to a plan, and every action it takes gets written down with enough detail to explain itself later. So if a server instance restarts mid-task, it just picks up where it left off — nothing is lost, and even if the same event arrives twice, the outcome only happens once.

Keeping that record accurate starts with how new information gets in. Gmail changes reach the service through Pub/Sub, Google's notification service; Calendar changes arrive through a separate, directly authenticated webhook. Either way, the actual work goes onto Cloud Tasks, Google's background task queue, and every step of that job is saved as it happens — if the worker crashes partway through, it resumes exactly where it stopped, without skipping or repeating a step. Calendar writes get the same discipline. They don't happen live, in the middle of handling a request; they go into a queue and are applied by one dedicated process, which checks the last version of that event it actually saw before writing. If the calendar changed in the meantime, the system re-reads it first instead of blindly overwriting it.

But all of that depends on the notification actually arriving. So a separate background job runs on a fixed schedule, independent of any push signal: it keeps the Gmail and Calendar subscriptions alive, catches up on any notification that got lost, and — once every minute — rechecks the whole schedule for conflicts on its own. Even if every push notification failed at once, the system would still catch and fix the problem within about a minute, without anyone needing to notice first.

A system with this much standing access to a real inbox and calendar has to be trusted not to misuse it, so every way into it is locked separately. The dashboard needs a session cookie plus a CSRF token, so a forged request from another site can't do anything. Internal task handlers accept only requests carrying a Google-signed service identity — nothing else gets through. The Calendar webhook is rate-limited, and the only thing it's allowed to do is trigger a re-fetch, never a write. The read-only judge demo is fully isolated, with no path to live data or real credentials at all. And every credential and token the system needs lives in Secret Manager, Google's encrypted secrets store — never in code, never in a config file.

That same discipline is why six days worked. I compressed a 21-day build plan into six, and I didn't call a stage done just because local tests passed — each one only closed once I had live evidence from the actual deployed service. Underneath all of that sit 224 automated tests that run the real production code against a simulated version of Firestore.

## Challenges we ran into

- **One bad commitment could poison the whole schedule.** In a deployed test, a single overdue item marked my *entire* set of active commitments as unworkable — and, as an unintended side effect, disabled the automatic-repair path meant to handle exactly that kind of problem. Local tests didn't catch it; a live run did. It's now a permanent regression test.
- **The model was more cautious than its creator.** In a live email thread, a deadline was negotiated ("Could I get it to you by Tuesday?" → "Could we bring the review forward to Monday at 4?"), and Gemini refused to lock in either date until an explicit acceptance appeared in the thread. My first reaction was that this was a model failure. It wasn't — the system was correctly distinguishing a *proposed* deadline from an *agreed* one.
- **A split-brain credential incident.** While debugging, I accidentally signed in with the wrong Google account, and the system started using that account's credentials going forward. Older server instances, which still had the previous credentials cached, kept seeing one person's Gmail and Calendar; newly started instances saw a different person's. For a while, the system was quietly split between two different realities. The audit trail reconstructed exactly what happened, minute by minute. No data was corrupted, and fixing it needed no special repair code — just turning off the bad credential and letting every instance resync.
- **Provider APIs have sharp edges.** Gmail has undocumented naming quirks, Gemini's structured-output format has real restrictions, and Cloud Tasks quietly reuses task names for 24 hours in a way that can mask a bug. Each one caused a real, reproducible failure. All three are now explicitly handled and covered by tests.

## Accomplishments that we're proud of

- **10/10 deployed end-to-end runs passed**, 61 checkpoints each, with live Gemini interpretation every time — the full loop from detecting a commitment in email through confirming effort, scheduling real Calendar events, injecting a conflict, watching it self-repair, logging verified check-ins, and marking the work complete.
- **9.1 seconds average automatic repair** — from the moment a conflicting meeting appears on the calendar to the moment the schedule is corrected, 7.2–10.2 seconds across all ten runs.
- **32/32 extraction test cases passed** — correctly identifying who owns a commitment and its deadline, producing valid structured output, and resisting prompt-injection attempts, all at roughly $0.0008 per email processed.
- **61/61 separate live security checks passed** against the deployed service — including attempts to reuse invalid sessions, forge requests without a valid CSRF token, and call internal endpoints with the wrong service identity, plus a full sweep confirming the judge-mode demo truly cannot change anything.
- **Provably safe to retry.** Re-delivering every single background job and action, on purpose, produces byte-for-byte the same result — proof that duplicate or repeated deliveries never corrupt state.
- **Built solo in six days** instead of the planned 21, with 224 automated tests and a complete audit trail of every decision the system made.

## What we learned

- **Put the model exactly where language lives, and nowhere else.** Gemini is well suited to resolving ambiguity in *who* promised *what* by *when*; deterministic code is much better suited to identity matching, scheduling constraints, policy, and anything that actually changes state.
- **Verbatim evidence is both explainability and a defense.** Requiring every extracted fact to point back to an exact quote in the source email gave the system a built-in audit trail *and* a strong defense against instructions hidden inside email content trying to manipulate the model — that defense held across every adversarial test case.
- **"Exactly once" is a product outcome, not an infrastructure setting.** The underlying guarantee is only "at least once" — the same email or calendar change can, in principle, get delivered and processed more than once. Turning that into "happens exactly once" in practice took several things working together: a unique ID per job, a check on which version of the data we're touching, a stable calendar event ID, and a lock that expires automatically if something crashes mid-task. I could prove it worked, not just hope it did, by deliberately re-delivering every message and checking that literally nothing changed the second time.
- **Short, resumable steps beat one long-running agent.** Every wait the system does is written down as a durable record. That means a recycled server instance, a paused task queue, or a user who replies tomorrow instead of today all look exactly the same to the system: nothing special happens, it just picks back up.

## What's next for CommitmentOS

The six-day build deliberately traded breadth for depth: get one commitment loop — detect, reserve, adapt, verify — fully reliable before adding more surfaces. Now that it has survived repeated live runs against the deployed service, the natural next steps are widening that loop: planning that understands dependencies between tasks, effort estimates that improve from verified history instead of staying fixed, and more sources feeding in and out (school assignments from Canvas, email from Outlook alongside Gmail). The core of the system — evidence-backed detection, conflict-free planning, minimal automatic repair, and completion that only a human can confirm — stays exactly as it is.
