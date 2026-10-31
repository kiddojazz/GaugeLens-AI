import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "./api";
import { useAuth } from "./auth";
import { ErrorBoundary, Loading } from "./components";
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

function Account() {
  const { me, signOut } = useAuth();
  const { pathname } = useLocation();
  if (!me) return null;
  if (me.kind === "guest") {
    return pathname === "/account" ? null : <Link className="btn small-btn" to={`/account?next=${encodeURIComponent(pathname)}`}>Sign in</Link>;
  }
  return (
    <div className="row account">
      {me.kind === "admin" && <span className="badge admin">Admin</span>}
      <span className="small muted account-email" title={me.email ?? undefined}>{me.email}</span>
      <button className="btn small-btn" onClick={() => void signOut()}>Sign out</button>
    </div>
  );
}

/** Guests are told, on every page, what happens to their data. */
function GuestBanner() {
  const { me, notice, dismissNotice } = useAuth();
  const { pathname } = useLocation();
  return (
    <>
      {notice && (
        <div className="notice action session-notice" role="status">
          <span>{notice}</span>
          <button className="link-btn" onClick={dismissNotice} aria-label="Dismiss">Dismiss</button>
        </div>
      )}
      {me?.kind === "guest" && pathname !== "/account" && (
        <div className="guest-banner" role="note">
          <strong>Private guest session{pathname === "/" ? ":" : "."}</strong>{" "}
          {pathname === "/" ? "scans are deleted when you leave." : <>Only you can see your scans and chat, and they're deleted when you
          refresh or close this page, or after {me.idle_minutes} minutes without activity.</>}{" "}
          <Link to={`/account?next=${encodeURIComponent(pathname)}&mode=register`}>Create a free account</Link> to keep them.
        </div>
      )}
    </>
  );
}

export default function Layout() {
  const { me, error } = useAuth();
  const { pathname } = useLocation();
  const admin = me?.kind === "admin";
  return (
    <div className="app">
      <aside className="sidebar">
        <Link className="brand" to="/" aria-label="GaugeLens home">
          <div className="brand-mark" aria-hidden>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round">
              <path d="M5 16a7 7 0 1 1 14 0" />
              <path d="M12 16 16 10" />
            </svg>
          </div>
          <div>
            <div className="brand-name">GaugeLens</div>
            <div className="brand-sub">Verified readings from any digital display</div>
          </div>
        </Link>
        <nav className="nav">
          <NavLink to="/" end><Icon name="home" />Home</NavLink>
          <NavLink to="/scan"><Icon name="scan" />Scan</NavLink>
          <NavLink to="/incidents"><Icon name="incidents" />Incidents</NavLink>
          <NavLink to="/clips"><Icon name="clips" />Clips</NavLink>
          <NavLink to="/upload"><Icon name="upload" />Upload</NavLink>
          <NavLink to="/equipment"><Icon name="equipment" />Equipment</NavLink>
          {admin && (
            <>
              <div className="nav-label">Admin</div>
              <NavLink to="/people"><Icon name="people" />People</NavLink>
              <NavLink to="/traces"><Icon name="traces" />Traces</NavLink>
              <NavLink to="/evals"><Icon name="evals" />Evaluations</NavLink>
            </>
          )}
        </nav>
        <div className="sidebar-foot">Readings checked against physics. A person approves every action.</div>
      </aside>
      <div className="main">
        <header className="topbar">
          <span className="muted small topbar-title">Meta Global AI Developer Hackathon 2026</span>
          <div className="row" style={{ gap: 14 }}>
            <HealthIndicator />
            <Account />
          </div>
        </header>
        <main className="content">
          <GuestBanner />
          {/* keyed by session: a new session (sign in, sign out, guest expiry) reloads every page's data */}
          {me ? <ErrorBoundary key={pathname}><Outlet key={me.session_id} /></ErrorBoundary> : error ? <div className="error">Could not start a session: {error}</div> : <Loading />}
        </main>
      </div>
    </div>
  );
}
