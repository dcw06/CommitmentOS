# LinkedIn post draft

Paste the block below as plain text — LinkedIn does not render markdown, so
the emoji act as bullet markers and nothing uses bold/asterisks. Fill the two
link placeholders before posting; keep the hashtag verbatim (required for
hackathon bonus points).

---

I promise things over email all the time — and then the deadline just sits in my inbox. Nothing actually reserves the hours to get the work done.

So for Google's All Things Agentic Hackathon, I built CommitmentOS: an AI agent that reads my Gmail, notices when I've made a commitment, books the work time on my Google Calendar, and quietly fixes the schedule when meetings collide with it. I stay in charge — it asks before its first calendar write, and only I can say work happened or mark something done.

What it does, with the receipts:

🏗️ Built it solo in 11 days on Google's Gemini 3.5 Flash and Agent Development Kit, running on Cloud Run with Firestore, Pub/Sub, and Cloud Tasks.

📬 Understands real email language: when someone asks, that's a request — when I reply "yes, I'll do it," it becomes my commitment, updating the same record instead of creating a second one. Zero duplicate commitments and zero duplicate calendar events across all test runs.

⚡ Repairs conflicts on its own in about 9 seconds (measured 7.2–10.2s on the live system): a meeting lands on reserved work time, the agent notices through the calendar's change feed and moves exactly one block — nobody clicks "replan."

🔁 Passed 10 consecutive end-to-end runs against the live deployment — real Gmail messages in, real Calendar events out, every one of 61 automated checks green each run. Then passed 10 more after a hardening pass.

🛡️ Passed all 73 live security checks on the deployed service, and correctly resisted emails containing hidden malicious instructions — 32 of 32 language-understanding test cases at 100%, at about $0.0008 per email interpreted.

⏱️ Refuses to guess: time passing never counts as work done. Only the minutes I explicitly confirm reduce what's left, and only I can mark a commitment complete.

🕹️ Lets anyone try it: the interactive sandbox runs the agent's real brain on a simulated inbox and calendar — no sign-in, right in the browser.

It doesn't plan once. It keeps the promise feasible as reality changes.

Try it yourself: [demo link]
Full write-up: [Devpost link]

#AllThingsAgenticHackathon
