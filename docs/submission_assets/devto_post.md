# Dev.to post draft

Paste into a new Dev.to post. Suggested tags: `googlecloud`, `gemini`, `agents`, `hackathon`.
Must be published PUBLIC (not draft/unlisted) — the bonus requires it — and the
required hackathon-entry sentence below must stay verbatim.

---

# Building CommitmentOS: A Reliable Agent That Keeps Promises Feasible

*I created this article for the purpose of entering the All Things Agentic Hackathon.*

## The problem: commitments get buried in email

I make promises over email all the time, but my calendar doesn’t automatically add them to my plan, and I keep forgetting my tasks. That’s why, for Google's All Things Agentic Hackathon (Taskmaster Track), I built CommitmentOS: an AI agent that finds commitments you make in Gmail, books work time on your Calendar, and repairs the plan when it detects a schedule conflict. I stay in charge — CommitmentOS asks for my permission before it writes its first calendar plan, and only I can mark work done.

## Why this is an agent rather than a chatbot

There’s no chat window here, and nothing waits around for me to ask it to do something.

CommitmentOS works behind the scenes: Gmail updates come in through Pub/Sub, Calendar changes arrive through a webhook, and each new event kicks off a check to see if anything needs to change. If a meeting overlaps with a work block, I don’t have to open anything—the calendar update itself triggers a fix. The system moves the work block and makes sure everything lines up with what I intended.

I’m still in control of the things that really need a human touch—deciding how long something will take, approving the first calendar entry, accepting new deadlines, logging my finished minutes, and marking work as done. Everything else happens on its own. Just letting time go by doesn’t count as progress; only the minutes I actually confirm determine what’s left to do.

## “Gemini interprets; deterministic code acts”

This is the rule that makes the system trustworthy: Gemini 3.5 Flash only reads and interprets language. Anything that actually touches my calendar is deterministic, tightly controlled, and can always be replayed.

The model pulls out who promised what, to whom, and by when—always linking each promise to the exact words in the message. Before anything actually happens, the output goes through strict checks: structure, meaning, and proof that it found the right quote. If Gemini offers more than one answer (sometimes it repeats itself), the system just picks the one that passes every check. Anything that doesn’t fit gets rejected and recorded, not patched up.

A deterministic identity resolver keeps requests and commitments straight: if someone asks me for something, that’s a request. When I say “yes, I’ll do it,” that turns into my commitment—no duplicates.

## The Gmail and Calendar control loop

The whole system runs in a loop: **Observe → Interpret → Plan → Policy → Execute & Verify → back to Observe.**

Here’s what that looked like in Cloud Logging during one real repair, end to end:

```
04:10:24  POST /webhooks/calendar          204   ← Google's push: a meeting appeared
04:10:24  POST /internal/tasks/source-sync 200   ← the change is pulled in
04:10:26  POST /internal/tasks/reconcile-observation 200  ← the repair is decided
04:10:41  POST /internal/tasks/execute-calendar-action 200 ← the fix is pushed back
04:10:43  POST /internal/tasks/reconcile-observation 200  ← desired vs actual verified
04:10:46  POST /webhooks/calendar          204   ← the agent's own write echoes back
```

It took just twenty-two seconds to go from a problem to a verified fix—and my favorite part is that the agent’s own calendar update comes back through the same webhook as a new event. The loop really closes. Nothing gets missed because nothing relies on me catching it at the right moment.

## Firestore, Cloud Tasks, Pub/Sub, Scheduler, and Cloud Run

* Cloud Run hosts the whole service.
* Firestore owns the truth. Every commitment, work block, observation, and decision gets saved as a durable document, and every decision is tracked on an audit timeline.
* Pub/Sub carries Gmail push notifications; an authenticated webhook carries Calendar changes.
* Cloud Tasks keeps things moving, with every step as a named, idempotent task—sync, reconcile, execute. If something crashes halfway through, the task just runs again and ends up right where it should, without any duplicates.
* Cloud Scheduler acts as a safety net: it watches for renewals, catches up on anything missed, and runs a sweep every minute to keep things in sync. If a push notification gets lost, it just means up to a sixty-second delay—not a silent failure.

## Reading the architecture diagram

![CommitmentOS architecture — signal-to-observation control loop](https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/6hcem1vn8r3t7fc735vi.png)

The numbered arrows (1–10) show how a single cycle through the whole system works, step by step.

**Ingestion (1–5):** Gmail and Calendar notifications don’t carry the actual data. The system authenticates the alert and queues a job to fetch the real changes. Cloud Tasks pulls the updates and saves them in Firestore as permanent records. If something crashes in the middle, it just picks up where it left off—nothing gets skipped.

**Reconciliation (6):** Every observation triggers a series of steps—Interpret, Plan, and Policy. Gemini suggests what it thinks is happening, quoting the exact sentence it found. The system then checks if the answer is valid. If the decision is within my approved limits, it takes action; if it’s more complicated, it asks me first.

**Action (7–10):** When it’s time to act, the plan goes to an outbox first, carrying an expected revision, ETag, and idempotency key. Before making any changes, the executor does a final check: if the calendar has changed since the plan was made, it throws away the old plan and makes a new one, so nothing out of date gets applied. The outcome goes back into the loop as a new observation, so the agent even watches its own changes.

**Safety net:** a once-a-minute Scheduler sweep means a lost push becomes a sixty-second delay, not a silent failure.

Any part of the system can crash halfway through, try something twice, or act on old info—the design just brings everything back in line instead of breaking.

## How idempotency, ETags, and recovery keep things safe

There are four main ways the system stays resilient when things go wrong:

1. Every outbound calendar action uses an **idempotency key**, so if something gets delivered twice, the result is always the same.
2. **ETag preconditions** (`If-Match`) are checked right before every write. If things have changed in the meantime (a 412 error), the system just tosses out the old plan instead of trying to force it through.
3. **Revision fences:** both plans and calendar events have version numbers, so if an action is based on old information, it gets rejected instead of overwriting something newer.
4. **Stable block identity:** when something needs to move, the system just updates the same calendar event instead of deleting and recreating it. That way, repairs only change what’s needed, everything else stays put, and the audit trail shows exactly what happened.

“Exactly once” turned out to be something the product actually achieves, not just a setting. I proved it by purposely re-delivering every observation and action, then checking that the saved state was exactly the same—down to the last byte.

## Results: 20 successful runs, ~9-second repair, zero duplicates

* I ran 10 live end-to-end tests on the deployed service—real Gmail, real Calendar events, and 61 automated checkpoints each, all passing. Then I did 10 more seeded runs after making the system more robust, with 50 checkpoints each.
* It took between 7.2 and 10.2 seconds (average 9.1 seconds) to go from detecting a conflict to fixing it on the live system.
* There were zero duplicate commitments and zero duplicate calendar events in every single test.
* All 73 live security checks passed on the real service; language understanding tests were perfect too—including resisting injection attacks—and it all cost about $0.0008 per message.

Every number above is linked to the exact Cloud Run revision in the repository’s `docs/proof_index.md`, with evidence files saved—including the failures.

## What I learned and where to go next

* **Google keeps Calendar event IDs forever.** If you try to recreate an event with an ID that was already canceled, you’ll inherit its “corpse.” Now, the executor detects this situation and revives the event using `events.update` with an `If-Match` check.
* **Calendar push notifications can get throttled after a burst of changes.** The once-a-minute reconciliation ensures that if any notifications are lost, it's just a short, predictable delay—not a silent problem.
* **Always validate the model’s output**—don’t just trust it or throw it out. The narrowing approach let me use good Gemini results without ever weakening the checks.
* **Build the audit trail first**—every tricky bug I ran into was eventually solved by looking at the detailed timeline the system records about itself.

Up next: handling dependencies between commitments, improving effort estimates by learning from past work, and supporting even more sources (like Outlook and Canvas) in the same loop. But the main principles—backing up every detection with evidence, making minimal repairs, and leaving completion to humans—stay the same.

## Links

- **Try it (no sign-in):** https://commitmentos-2hscowvydq-uw.a.run.app/sandbox — the real stack over an isolated in-memory world
- **4-minute demo:** https://www.youtube.com/watch?v=oo3uoq9IXJ4
- **Submission:** https://devpost.com/software/closeyoureyes
- **Code and evidence:** https://github.com/dcw06/CommitmentOS

It doesn’t plan once. It keeps the promise feasible as reality changes.