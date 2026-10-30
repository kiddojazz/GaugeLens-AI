import { Link } from "react-router-dom";
import { useAuth } from "./auth";
import { Component, type ErrorInfo, type ReactNode } from "react";

type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className="badge">
      {tone !== "neutral" && <span className={`dot ${tone}`} />}
      {children}
    </span>
  );
}

const STATUS_TONES: Record<string, Tone> = {
  done: "ok", accepted: "ok", pass: "ok", approved: "ok", corrected: "info", misread_fixed: "info",
  processing: "info", queued: "neutral", running: "info", pending: "neutral",
  needs_review: "warn", warning: "warn", rejected: "bad", failed: "bad", dead: "bad",
  persistent_violation: "bad", critical: "bad", info: "neutral", verified: "ok",
};

const STATUS_LABELS: Record<string, string> = {
  misread_fixed: "Misread fixed", persistent_violation: "Confirmed violation", needs_review: "Needs review",
};

export function Status({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">—</span>;
  const label = STATUS_LABELS[value] ?? value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, " ");
  return <Badge tone={STATUS_TONES[value] ?? "neutral"}>{label}</Badge>;
}

export function Simulated() {
  return <span className="tag" title="A labelled test fault, not real equipment">Simulated</span>;
}

export function Card({ title, actions, children, pad = true }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; pad?: boolean }) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card-head">
          {typeof title === "string" ? <h2>{title}</h2> : title}
          {actions}
        </div>
      )}
      <div className={pad ? "card-body" : undefined}>{children}</div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="error">{error}</div> : null;
}

export function Loading() {
  return <div className="empty">Loading…</div>;
}

export function Progress({ value }: { value: number }) {
  return (
    <div className="progress" aria-label={`${Math.round(value * 100)} %`}>
      <div style={{ width: `${Math.max(2, Math.min(100, value * 100))}%` }} />
    </div>
  );
}

/** A small inline trend line (no chart library). */
export function Sparkline({ values, width = 160, height = 36 }: { values: number[]; width?: number; height?: number }) {
  if (values.length < 2) return <span className="muted small">—</span>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const y = (v: number) => (span ? height - 2 - ((v - min) / span) * (height - 4) : height / 2);   // a steady value sits mid-height, not on the floor
  const points = values
    .map((v, i) => `${((i / (values.length - 1)) * (width - 4) + 2).toFixed(1)},${y(v).toFixed(1)}`)
    .join(" ");
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="trend">
      <polyline points={points} fill="none" stroke="var(--accent)" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

/** Admin pages: everyone else gets a short explanation (the API refuses them too). */
export function AdminOnly({ children }: { children: ReactNode }) {
  const { me } = useAuth();
  if (me?.kind === "admin") return <>{children}</>;
  return <div className="empty">This page is for administrators. <Link to="/account">Sign in</Link> with an admin account.</div>;
}

/** A page that throws while rendering shows this, and the sidebar stays usable, instead of a blank app.
 *  Not "reload": that would end a guest's session and delete their scans. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("page failed to render", error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="error" role="alert">
        <p style={{ marginTop: 0 }}><strong>This page couldn't be shown.</strong> Your data is safe; pick another page from the menu, or try again.</p>
        <button className="btn" onClick={() => this.setState({ failed: false })}>Try again</button>
      </div>
    );
  }
}
