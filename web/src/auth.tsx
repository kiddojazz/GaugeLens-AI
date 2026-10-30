// Who is using the app. On load: an account cookie signs the person in; otherwise a fresh guest session starts.
// A guest session ends (and the server deletes its scans and chat) when the page is refreshed or closed, or after
// the idle timeout without any activity. Signing in or creating an account keeps the guest's scans.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, endGuestSession, getGuestToken, setGuestToken, SESSION_ENDED, type Me } from "./api";

type Auth = {
  me: Me | null;
  error: string | null;
  notice: string | null;
  dismissNotice: () => void;
  signedIn: (me: Me) => void;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
};

const AuthContext = createContext<Auth | null>(null);
const ACTIVITY = ["pointerdown", "keydown", "scroll", "touchstart"] as const;

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth outside AuthProvider");
  return auth;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const starting = useRef<Promise<void> | null>(null);
  const lastActive = useRef(Date.now());

  const startGuest = useCallback((why?: string) => {
    if (starting.current) return starting.current;         // one session even if several requests fail at once
    starting.current = (async () => {
      try {
        const old = getGuestToken();
        if (old) endGuestSession(old);
        setGuestToken(null);
        const guest = await api.startGuest();
        setGuestToken(guest.token);
        const { token: _token, ...rest } = guest;
        setMe(rest);
        setError(null);
        if (why) setNotice(why);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        starting.current = null;
      }
    })();
    return starting.current;
  }, []);

  const refresh = useCallback(async () => {
    try {
      setMe(await api.me());
    } catch {
      /* the session-ended handler takes over */
    }
  }, []);

  // first load: the account cookie, or a new guest session
  useEffect(() => {
    let cancelled = false;
    api.me().then((m) => { if (!cancelled) setMe(m); }).catch(() => { if (!cancelled) void startGuest(); });
    return () => { cancelled = true; };
  }, [startGuest]);

  // a 401 from any page: the session ended on the server (idle, or this page was restored from the back cache)
  useEffect(() => {
    const ended = () => {
      const wasGuest = !!getGuestToken();
      void startGuest(wasGuest
        ? "Your guest session ended, so its scans and chat were deleted. This is a new session."
        : "You were signed out. Sign in again to see your scans.");
    };
    window.addEventListener(SESSION_ENDED, ended);
    return () => window.removeEventListener(SESSION_ENDED, ended);
  }, [startGuest]);

  // a guest's session ends with the page
  useEffect(() => {
    const leave = () => { const token = getGuestToken(); if (token) endGuestSession(token); };
    window.addEventListener("pagehide", leave);
    return () => window.removeEventListener("pagehide", leave);
  }, []);

  // idle timeout: the page polls in the background, so the server alone can't tell an idle guest from an active one
  useEffect(() => {
    if (me?.kind !== "guest") return;
    const active = () => { lastActive.current = Date.now(); };
    ACTIVITY.forEach((e) => window.addEventListener(e, active, { passive: true }));
    active();
    const id = window.setInterval(() => {
      if (Date.now() - lastActive.current > me.idle_minutes * 60_000) {
        void startGuest(`Your guest session ended after ${me.idle_minutes} minutes without activity, so its scans and chat were deleted.`);
      }
    }, 30_000);
    return () => {
      ACTIVITY.forEach((e) => window.removeEventListener(e, active));
      window.clearInterval(id);
    };
  }, [me?.kind, me?.idle_minutes, me?.session_id, startGuest]);

  const signedIn = useCallback((m: Me) => {
    setGuestToken(null);                                   // the server already ended it and moved its scans
    setMe(m);
    setNotice(null);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      await startGuest("You're signed out. Your scans stay in your account.");
    }
  }, [startGuest]);

  return (
    <AuthContext.Provider value={{ me, error, notice, dismissNotice: () => setNotice(null), signedIn, signOut, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}
