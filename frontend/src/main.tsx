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

import { BASENAME, bootstrapSession, DEMO_MODE, logout } from "./api";
import { ActivityPage } from "./views/activity";
import { CommitmentDetailPage, CommitmentsPage } from "./views/commitments";
import { TodayPage } from "./views/today";

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
          capability.
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
      <Shell />
    </BrowserRouter>
  </StrictMode>,
);
