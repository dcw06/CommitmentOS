# LinkedIn post draft

Paste the block below as plain text — LinkedIn does not render markdown.
Keep the hashtag verbatim (required for hackathon bonus points).

---

I make promises over email all the time, but my calendar doesn't automatically put that into my plan, and I keep forgetting about my tasks. And that's why for Google's All Things Agentic Hackathon, I built CommitmentOS, an AI agent that finds the commitments you make in your Gmail, books the work time on your Calendar, and repairs the plan when it detects a schedule conflict. I stay in charge — CommitmentOS asks for my permission before it writes its first calendar plan, and only I can mark work done.

How it actually played out:

– I built this solo in two weeks, putting together Gemini 3.5 Flash, the Agent Development Kit, and Google Cloud tools—Cloud Run, Firestore, Pub/Sub, and Cloud Tasks.

– Whenever a meeting lands onto my reserved work time, the agent notices through the calendar's change feed and moves just that block — in about 9 seconds, without me having to replan anything.

– I ran 10 full end-to-end tests using live Gmail and Calendar—61 checks per run, all green. After tightening things up, I did 10 more. All passed. Even 73 security probes couldn't break it, and there wasn't a single duplicate commitment or event.

It doesn't plan once. It continuously adjusts its work blocks to keep your commitments on track as things change.

Try it yourself (no sign-in): https://commitmentos-2hscowvydq-uw.a.run.app/sandbox

Watch the 4-minute demo: https://www.youtube.com/watch?v=oo3uoq9IXJ4

Full write-up: https://devpost.com/software/closeyoureyes

#AllThingsAgenticHackathon
