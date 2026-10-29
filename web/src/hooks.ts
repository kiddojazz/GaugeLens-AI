import { useCallback, useEffect, useRef, useState } from "react";
import { eventsUrl } from "./api";

/** Fetches on mount (and when deps change); `reload` refetches without clearing the current data. */
export function useApi<T>(load: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, reload };
}

export type ClipEvent = { id: number; type: string; payload: Record<string, unknown> };

/** Live clip events over SSE (resumes after reconnects). Calls onEvent for each event. */
export function useClipEvents(clipId: string | undefined, onEvent: (event: ClipEvent) => void, enabled = true) {
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    if (!clipId || !enabled) return;
    const source = new EventSource(eventsUrl(clipId));
    const types = ["job_status", "displays_detected", "frame_processed", "reading", "read_failed", "physics_check",
      "relook_attempt", "anomaly", "diagnosis", "clip_done"];
    const listeners = types.map((type) => {
      const listener = (e: MessageEvent) => {
        try {
          handler.current({ id: Number(e.lastEventId), type, payload: JSON.parse(e.data) });
        } catch {
          /* ignore malformed events */
        }
      };
      source.addEventListener(type, listener);
      return [type, listener] as const;
    });
    return () => {
      listeners.forEach(([type, listener]) => source.removeEventListener(type, listener));
      source.close();
    };
  }, [clipId, enabled]);
}

/** Calls fn every `ms` while `active`. */
export function usePolling(fn: () => void, ms: number, active: boolean) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(id);
  }, [ms, active]);
}
