"""Judge-facing interactive sandbox.

Runs the real CommitmentOS command stack (interpretation, identity
resolution, planning, policy, execution, audit) over an isolated in-memory
twin per session. No Google credential, Firestore client, or live-user
document is reachable from this package's composition by construction:
`SandboxWorld` accepts only the twin adapters plus a model interpreter.
"""
