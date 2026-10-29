import { NavLink, Outlet } from "react-router-dom";
import { api } from "./api";
import { useApi, usePolling } from "./hooks";
import { Icon } from "./icons";

function HealthIndicator() {
  const { data, error, reload } = useApi(() => api.health(), []);
  usePolling(reload, 30000, true);
  const ok = data?.status === "ok";
  return (
    <div className="row small muted" title={error ?? undefined}>
      <span className={`dot ${error ? "bad" : ok ? "ok" : "warn"}`} />
      {error ? "API unreachable" : data ? `${data.model.model} · database ${data.db}` : "Connecting…"}
    </div>
  );
}

export default function Layout() {
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round">
              <path d="M5 16a7 7 0 1 1 14 0" />
              <path d="M12 16 16 10" />
            </svg>
          </div>
          <div>
            <div className="brand-name">GaugeLens</div>
            <div className="brand-sub">Verified readings from any camera</div>
          </div>
        </div>
        <nav className="nav">
          <NavLink to="/" end><Icon name="incidents" />Incidents</NavLink>
          <NavLink to="/clips"><Icon name="clips" />Clips</NavLink>
          <NavLink to="/upload"><Icon name="upload" />Upload</NavLink>
          <NavLink to="/equipment"><Icon name="equipment" />Equipment</NavLink>
          <div className="nav-label">Agent</div>
          <NavLink to="/traces"><Icon name="traces" />Traces</NavLink>
          <NavLink to="/evals"><Icon name="evals" />Evaluations</NavLink>
        </nav>
        <div className="sidebar-foot">Readings checked against physics. A person approves every action.</div>
      </aside>
      <div className="main">
        <header className="topbar">
          <span className="muted small">Meta Global AI Developer Hackathon 2026</span>
          <HealthIndicator />
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
