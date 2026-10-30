// Sign in or create an account. A guest who does either keeps the scans and chat from this visit.
import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Card, ErrorBox } from "../components";
import { Icon } from "../icons";

export default function AccountPage() {
  const { me, signedIn, signOut } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [mode, setMode] = useState<"login" | "register">(params.get("mode") === "register" ? "register" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const next = params.get("next");
  const back = next && next.startsWith("/") && next !== "/account" ? next : "/";

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const m = mode === "register" ? await api.register(email, password) : await api.login(email, password);
      signedIn(m);
      navigate(back);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (me && me.kind !== "guest") {
    return (
      <>
        <div className="page-head"><div><h1>Account</h1><p>Signed in as {me.email}.</p></div></div>
        <Card>
          <p style={{ marginTop: 0 }}>Your scans and conversations are kept in your account, and only you{me.kind === "admin" ? " (and other administrators)" : " and administrators"} can see them.</p>
          <button className="btn" onClick={() => void signOut()}>Sign out</button>
        </Card>
      </>
    );
  }

  const guestClips = me?.kind === "guest" && me.clips_left !== undefined;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{mode === "register" ? "Create an account" : "Sign in"}</h1>
          <p>Keep your scans and pick up your conversations later, on any device.</p>
        </div>
      </div>
      <div className="grid-2">
        <Card pad={false}>
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={mode === "login"} className={mode === "login" ? "active" : ""} onClick={() => setMode("login")}>Sign in</button>
            <button role="tab" aria-selected={mode === "register"} className={mode === "register" ? "active" : ""} onClick={() => setMode("register")}>Create account</button>
          </div>
          <form className="stack tab-body" onSubmit={submit}>
            <ErrorBox error={error} />
            <label className="field">
              Email
              <input type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </label>
            <label className="field">
              Password
              <input type="password" autoComplete={mode === "register" ? "new-password" : "current-password"} minLength={mode === "register" ? 8 : 1}
                     value={password} onChange={(e) => setPassword(e.target.value)} required />
              {mode === "register" && <span className="muted small" style={{ fontWeight: 400 }}>At least 8 characters.</span>}
            </label>
            <div className="row">
              <button className="btn primary" type="submit" disabled={busy}>
                <Icon name="lock" size={14} /> {mode === "register" ? "Create account" : "Sign in"}
              </button>
              {guestClips && <span className="muted small">Scans from this visit move to your account.</span>}
            </div>
          </form>
        </Card>
        <Card title="What happens to your data">
          <ul className="tips">
            <li><strong>As a guest,</strong> your scans, readings and chat are private to this page. They're deleted when you refresh or close it, or after {me?.idle_minutes ?? 30} minutes without activity.</li>
            <li><strong>With an account,</strong> they're kept so you can come back to a clip and continue the conversation.</li>
            <li><strong>Administrators</strong> can see scans to keep the service safe and working.</li>
            <li>Your password is stored only as a salted hash.</li>
          </ul>
        </Card>
      </div>
    </>
  );
}
