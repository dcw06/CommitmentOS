import "./styles.css";

import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  BrowserRouter,
  NavLink,
  Navigate,
  Route,
  Routes,
} from "react-router-dom";

import {
  BASENAME,
  bootstrapSession,
  DEMO_MODE,
  fetchSystemStatus,
  logout,
  SANDBOX_MODE,
} from "./api";
import { Badge, usePolling } from "./ui";
import { ActivityPage } from "./views/activity";
import { CommitmentDetailPage, CommitmentsPage } from "./views/commitments";
import { SandboxPage } from "./views/sandbox";
import { TodayPage } from "./views/today";

function LiveControlStatus() {
  const { data, error } = usePolling(fetchSystemStatus, 8000);
  if (error && !data) return <span className="status-error">Control status unavailable</span>;
  if (!data) return <span className="status-loading">Loading controls…</span>;
  return (
    <div className="global-controls" aria-label="Live execution control status">
      <span>Monitoring</span><Badge value={data.observationMode} />
      <span>Actions</span><Badge value={data.automaticActionMode} />
      <span>{data.heldActions} held · {data.inFlightActions} in flight</span>
    </div>
  );
}

/** The sandbox is its own surface: no session to bootstrap, no live nav, and
 *  a layout built around driving the scenario rather than reading a
 *  dashboard. */
function SandboxShell() {
  return (
    <>
      <div className="demo-banner sandbox-banner">
        <strong>Interactive sandbox</strong> — the real agent running on a
        simulated inbox and calendar that exist only in your session. No Google
        account or controlled-user data is used. Free-play text is sent to a
        sandbox-only Gemini key for live interpretation, held in this process
        until reset/expiry, and never written to Firestore or shared caches.
      </div>
      <header className="shell-header">
        <div className="brand">
          Commitment<span>OS</span>
        </div>
        <div className="shell-spacer" />
        <div className="shell-status">
          <a href="/demo">See the dashboard →</a>
        </div>
      </header>
      <main>
        <SandboxPage />
      </main>
    </>
  );
}

function Shell() {
  const [ready, setReady] = useState(DEMO_MODE);
  const [bootError, setBootError] = useState<string | null>(null);

  useEffect(() => {
    if (DEMO_MODE) return;
    bootstrapSession()
      .then(() => setReady(true))
      .catch((error: unknown) =>
        setBootError(error instanceof Error ? error.message : "session bootstrap failed"),
      );
  }, []);

  if (bootError) return <div className="loading">{bootError}</div>;
  if (!ready) return <div className="loading">Signing in…</div>;

  return (
    <>
      {DEMO_MODE && (
        <div className="demo-banner">
          <strong>Seeded judge mode</strong> — read-only demonstration data derived
          from a fixed scenario; no live mailbox content and no mutation
          capability. <a href="/sandbox">Want to drive it yourself? →</a>
        </div>
      )}
      <header className="shell-header">
        <div className="brand">
          Commitment<span>OS</span>
        </div>
        <nav className="shell-nav">
          <NavLink to="/" end>
            Today
          </NavLink>
          <NavLink to="/commitments">Commitments</NavLink>
          <NavLink to="/activity">Activity</NavLink>
        </nav>
        <div className="shell-spacer" />
        <div className="shell-status">
          {!DEMO_MODE && <LiveControlStatus />}
          {!DEMO_MODE && (
            <button onClick={() => void logout()} title="Revoke this session">
              Sign out
            </button>
          )}
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<TodayPage />} />
          <Route path="/commitments" element={<CommitmentsPage />} />
          <Route path="/commitments/:commitmentId" element={<CommitmentDetailPage />} />
          <Route path="/activity" element={<ActivityPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter basename={BASENAME}>
      {SANDBOX_MODE ? <SandboxShell /> : <Shell />}
    </BrowserRouter>
  </StrictMode>,
);
