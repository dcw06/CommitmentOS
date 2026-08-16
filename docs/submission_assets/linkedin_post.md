# LinkedIn post draft

I spent the last week building an AI agent that manages my actual commitments — for Google's All Things Agentic Hackathon.

The idea started from something annoying: I promise things over email all the time, but the deadline just sits in my inbox. Nothing reserves the hours to actually get it done.

So I built CommitmentOS. Gemini reads my Gmail threads and figures out who I promised what, to whom, and by when — even when the deadline gets renegotiated over a few replies. Deterministic code takes it from there: it reserves real time on my Calendar, and if a meeting lands on top of planned work, it repairs the plan on its own in about 9 seconds. I check in the minutes I actually worked, and I'm the one who marks it done — never the system guessing.

Six days, solo, one Cloud Run service, and ten straight end-to-end test runs against the live deployment before I called it done.

Live demo (no sign-in needed): [demo link]
Full write-up: [Devpost link]

#AllThingsAgenticHackathon
