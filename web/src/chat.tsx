// "Ask the agent": questions about a clip, next to the footage. Answers cite evidence; clicking a citation
// moves the viewer (clip page) or opens the clip at that moment (anomaly page).
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, type ChatMessage } from "./api";
import { shortId } from "./format";
import { useApi } from "./hooks";
import { Icon } from "./icons";

const EVIDENCE = /(?:reading|check|frame|anomaly):[A-Za-z0-9_#.\-]*[A-Za-z0-9]/g;
const BRACKETED = /\[\s*((?:(?:reading|check|frame|anomaly):[^\],\s]+\s*,?\s*)+)\]/g;

/** One line of answer text: evidence IDs become clickable chips, **bold** stays bold. */
function richLine(line: string, onCite: (ref: string) => void): ReactNode[] {
  const text = line.replace(BRACKETED, (_, ids: string) => ` ${ids.replace(/,/g, " ")} `);
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(EVIDENCE)) {
    out.push(...bold(text.slice(last, m.index)));
    const ref = m[0];
    const [kind, id] = ref.split(":");
    out.push(<button key={`${m.index}`} className="cite" title={ref} onClick={() => onCite(ref)}>{kind === "frame" ? frameName(id) : `${kind} ${shortId(id)}`}</button>);
    last = (m.index ?? 0) + ref.length;
  }
  out.push(...bold(text.slice(last)));
  return out;
}

/** **bold** and `code` (models like to wrap values in backticks). */
/** Frame IDs end in _f<index>: show them the way the viewer counts ("frame 5"). */
function frameName(frameId: string) {
  const m = frameId.match(/_f(\d+)$/);
  return m ? `frame ${Number(m[1]) + 1}` : `frame ${shortId(frameId)}`;
}

function bold(text: string): ReactNode[] {
  return text.split(/(\*\*.+?\*\*|`[^`]+`)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? <strong key={i}>{part.slice(2, -2)}</strong>
      : part.startsWith("`") && part.endsWith("`") && part.length > 2 ? <code key={i}>{part.slice(1, -1)}</code>
      : part);
}

function Answer({ text, onCite }: { text: string; onCite: (ref: string) => void }) {
  const blocks: ReactNode[] = [];
  let bullets: ReactNode[] = [];
  const flush = () => {
    if (bullets.length) blocks.push(<ul key={`ul${blocks.length}`}>{bullets}</ul>);
    bullets = [];
  };
  text.split("\n").forEach((raw, i) => {
    const line = raw.trim();
    const item = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (item) bullets.push(<li key={i}>{richLine(item[1], onCite)}</li>);
    else {
      flush();
      if (line) blocks.push(<p key={i}>{richLine(line, onCite)}</p>);
    }
  });
  flush();
  return <div className="answer">{blocks}</div>;
}

export function ChatPanel({ clipId, frameId, frameLabel, anomalyId, suggestions, onCite }: {
  clipId: string;
  frameId?: string | null;
  frameLabel?: string;
  anomalyId?: string;
  suggestions: string[];
  onCite: (ref: string) => void;
}) {
  const history = useApi(() => api.chat(clipId), [clipId]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useFrame, setUseFrame] = useState(true);
  const log = useRef<HTMLDivElement>(null);

  useEffect(() => { if (history.data) setMessages(history.data); }, [history.data]);
  useEffect(() => { setUseFrame(true); }, [frameId]);             // a new frame in view is context again
  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" }); }, [messages, busy]);

  async function send(question: string) {
    const q = question.trim();
    if (!q || busy) return;
    const frame = useFrame ? frameId ?? null : null;
    setBusy(true);
    setError(null);
    setDraft("");
    const pending: ChatMessage = {
      message_id: `local-${Date.now()}`, clip_id: clipId, role: "user", content: q, frame_id: frame,
      anomaly_id: anomalyId ?? null, citations: [], verified: null, problems: [], run_id: null, created_at: new Date().toISOString(),
    };
    setMessages((m) => [...m, pending]);
    try {
      const answer = await api.ask(clipId, q, frame, anomalyId);
      setMessages((m) => [...m, answer]);
    } catch (e) {
      setMessages((m) => m.filter((x) => x !== pending));
      setDraft(q);                                                  // nothing lost: try again
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    await api.clearChat(clipId);
    setMessages([]);
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send(draft);
    }
  }

  return (
    <section className="card chat">
      <div className="card-head">
        <h2 className="row" style={{ gap: 8 }}><Icon name="chat" /> Ask the agent</h2>
        {messages.length > 0 && <button className="btn small-btn" onClick={reset} disabled={busy}>New conversation</button>}
      </div>
      <div className="chat-log" ref={log} aria-live="polite">
        {!messages.length && !busy && (
          <div className="chat-empty">
            <p className="muted small">Ask about these readings, a correction or the anomaly. Answers cite the evidence; click a citation to see it.</p>
            <div className="suggestions">
              {suggestions.map((s) => <button key={s} className="suggestion" onClick={() => send(s)}>{s}</button>)}
            </div>
          </div>
        )}
        {messages.map((m) => m.role === "user" ? (
          <div key={m.message_id} className="bubble user">
            {m.content}
            {m.frame_id && <div className="bubble-meta">about {frameName(m.frame_id)}</div>}
          </div>
        ) : (
          <div key={m.message_id} className="bubble agent">
            <Answer text={m.content} onCite={onCite} />
            <div className="bubble-meta row" style={{ gap: 10 }}>
              {m.verified ? <span className="checked"><Icon name="check" size={12} /> Evidence-checked</span>
                : <span className="unchecked" title={m.problems.join("; ")}><Icon name="fallback" size={12} /> Couldn't verify everything</span>}
              {m.run_id && <Link to={`/traces/${m.run_id}`}>Trace</Link>}
            </div>
          </div>
        ))}
        {busy && <div className="bubble agent thinking" aria-label="The agent is thinking"><span /><span /><span /></div>}
      </div>
      {error && <div className="error" style={{ margin: "0 16px 10px" }}>{error}</div>}
      <div className="chat-input">
        {frameId && (
          <div className="chat-context">
            {useFrame ? (
              <span className="context-chip"><Icon name="frame" size={12} /> Asking about {frameLabel ?? "this frame"}
                <button aria-label="Don't include this frame" onClick={() => setUseFrame(false)}><Icon name="cross" size={11} /></button></span>
            ) : <button className="link-btn small" onClick={() => setUseFrame(true)}>+ Include the frame in view</button>}
          </div>
        )}
        <div className="chat-compose">
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKey} rows={2} maxLength={1000}
                    placeholder="Ask a question…" aria-label="Your question" disabled={busy} />
          <button className="btn primary" onClick={() => send(draft)} disabled={busy || !draft.trim()} aria-label="Send">
            <Icon name="send" size={14} />
          </button>
        </div>
      </div>
    </section>
  );
}
