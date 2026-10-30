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

// --- voice: the browser's own speech recognition and synthesis (no extra service). Hidden where unsupported.
type Recognition = { lang: string; interimResults: boolean; continuous: boolean; start(): void; stop(): void;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null;
  onend: (() => void) | null; onerror: ((e: { error: string }) => void) | null };
const RecognitionCtor: (new () => Recognition) | undefined =
  typeof window !== "undefined" ? ((window as never)["SpeechRecognition"] ?? (window as never)["webkitSpeechRecognition"]) : undefined;
const canSpeak = typeof window !== "undefined" && "speechSynthesis" in window;
const SPEAK_KEY = "gaugelens.readAloud";

/** What an answer sounds like: no evidence IDs, no markdown. */
export function spoken(text: string): string {
  return text.replace(BRACKETED, " ").replace(EVIDENCE, " ").replace(/[*`#]/g, "").replace(/^\s*[-•]\s+/gm, "")
    .replace(/\s+([,.;:])/g, "$1").replace(/\s{2,}/g, " ").trim();
}

// Phones only allow speech that starts from a tap, and an answer arrives seconds after one. Speaking anything during
// the tap unlocks speech for the page, so the answer may be read when it comes.
function unlockSpeech() {
  if (!canSpeak || window.speechSynthesis.speaking || window.speechSynthesis.pending) return;
  const silent = new SpeechSynthesisUtterance(" ");
  silent.volume = 0;
  window.speechSynthesis.speak(silent);
}

let speaking: SpeechSynthesisUtterance[] = [];   // browsers can drop an utterance nothing refers to, and its events

/** Reads text sentence by sentence (Chrome cuts off long utterances). onEnd(null) = finished or stopped. */
function speakText(text: string, onEnd: (problem: string | null) => void) {
  const synth = window.speechSynthesis;
  const parts = text.match(/(?:\d[.,]\d|[^.!?\n])+[.!?]*/g)?.map((t) => t.trim()).filter(Boolean) ?? [];
  synth.cancel();
  speaking = parts.map((part) => new SpeechSynthesisUtterance(part));
  if (!speaking.length) return onEnd(null);
  const current = speaking;
  current.forEach((u, i) => {
    u.lang = navigator.language || "en-US";
    if (i === current.length - 1) u.onend = () => onEnd(null);
    u.onerror = (e) => onEnd(e.error === "canceled" || e.error === "interrupted" ? null
      : "This browser couldn't read the answer aloud. Check the volume and silent switch, or that the phone has a text-to-speech voice installed.");
    synth.speak(u);
  });
  synth.resume();                                // Chrome can sit paused after the tab was in the background
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
  const [listening, setListening] = useState(false);
  const [readAloud, setReadAloud] = useState(() => {
    try { return window.localStorage.getItem(SPEAK_KEY) === "1"; } catch { return false; }
  });
  const [speakingId, setSpeakingId] = useState<string | null>(null);
  const recognition = useRef<Recognition | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useFrame, setUseFrame] = useState(true);
  const log = useRef<HTMLDivElement>(null);

  useEffect(() => { if (history.data) setMessages(history.data); }, [history.data]);
  useEffect(() => { setUseFrame(true); }, [frameId]);             // a new frame in view is context again
  useEffect(() => () => { if (canSpeak) window.speechSynthesis.cancel(); }, []);   // stop when leaving the page
  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" }); }, [messages, busy]);

  function say(message: ChatMessage) {
    setError(null);
    setSpeakingId(message.message_id);
    speakText(spoken(message.content), (problem) => {
      setSpeakingId((id) => (id === message.message_id ? null : id));
      if (problem) setError(problem);
    });
  }

  function stopSpeaking() {
    window.speechSynthesis.cancel();
    setSpeakingId(null);
  }

  async function send(question: string) {
    const q = question.trim();
    if (!q || busy) return;
    if (readAloud) unlockSpeech();
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
      if (readAloud && canSpeak) say(answer);
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

  function toggleReadAloud() {
    const next = !readAloud;
    setReadAloud(next);
    if (canSpeak) {
      if (next) {
        const latest = [...messages].reverse().find((m) => m.role === "assistant");
        if (latest) say(latest); else unlockSpeech();   // this tap also allows later answers to be read
      } else {
        stopSpeaking();
      }
    }
    try { window.localStorage.setItem(SPEAK_KEY, next ? "1" : "0"); } catch { /* private mode: not remembered */ }
  }

  /** Speak a question: the words fill the box as you talk, and it is sent when you stop. */
  function listen() {
    if (!RecognitionCtor) return;
    if (listening) {
      recognition.current?.stop();
      return;
    }
    const rec = new RecognitionCtor();
    rec.lang = navigator.language || "en-US";
    rec.interimResults = true;
    rec.continuous = false;
    let heard = "";
    rec.onresult = (e) => {
      heard = Array.from(e.results).map((r) => r[0].transcript).join(" ").trim();
      setDraft(heard);
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed") setError("Microphone permission was refused. Allow it for this site to ask by voice.");
    };
    rec.onend = () => {
      setListening(false);
      recognition.current = null;
      if (heard) void send(heard);
    };
    recognition.current = rec;
    setListening(true);
    if (canSpeak) {
      window.speechSynthesis.cancel();                             // don't talk over the question
      setSpeakingId(null);
      if (readAloud) unlockSpeech();
    }
    rec.start();
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
        <div className="row" style={{ gap: 6 }}>
          {canSpeak && (
            <button className={`btn small-btn${readAloud ? " active" : ""}`} onClick={toggleReadAloud} aria-pressed={readAloud}
                    title="Read answers aloud"><Icon name="speaker" size={13} /> {readAloud ? "Reading aloud" : "Read aloud"}</button>
          )}
          {messages.length > 0 && <button className="btn small-btn" onClick={reset} disabled={busy}>New conversation</button>}
        </div>
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
              {canSpeak && (speakingId === m.message_id
                ? <button className="link-btn" onClick={stopSpeaking}>Stop</button>
                : <button className="link-btn" onClick={() => say(m)} aria-label="Listen to this answer"><Icon name="speaker" size={12} /> Listen</button>)}
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
                    placeholder={listening ? "Listening… speak your question" : "Ask a question…"} aria-label="Your question" disabled={busy} />
          {RecognitionCtor && (
            <button className={`btn mic${listening ? " listening" : ""}`} onClick={listen} disabled={busy}
                    aria-label={listening ? "Stop listening" : "Ask by voice"} aria-pressed={listening}>
              <Icon name="mic" size={15} />
            </button>
          )}
          <button className="btn primary" onClick={() => send(draft)} disabled={busy || !draft.trim()} aria-label="Send">
            <Icon name="send" size={14} />
          </button>
        </div>
      </div>
    </section>
  );
}
