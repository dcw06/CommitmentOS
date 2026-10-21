# Portfolio maintenance — 2026-10-21

Decision: provide a separate, credential-free local read-only preview using the existing `DemoRouter` and `StaticDemoReadModel`. It mounts the built dashboard without constructing the production application container. Live-account APIs and the interactive sandbox are absent from this entry point.

The production factory, cloud adapters, policies, frozen scenario data, historical evidence, and deployment settings are unchanged. No deployment, model call, Google API write, or cloud campaign was performed.

Validation: 329 backend tests passed, including the new preview isolation/mutation regression; Ruff passed; three frontend tests passed; the TypeScript/Vite build passed. The new GitHub Actions workflow repeats credential-free backend and frontend checks. Historical cloud measurements remain bound to their original revisions in the proof index.
