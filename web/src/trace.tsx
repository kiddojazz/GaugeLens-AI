// Agent trace view (Langfuse / LangSmith style): a step tree with timings on the left, the selected step's
// input, output and attributes on the right.
import { useState, type ReactNode } from "react";
import { evidenceUrl, type AgentRun, type Span } from "./api";
import { Badge } from "./components";
import { Icon, KIND_LABEL, spanIcon } from "./icons";

export function duration(ms: number | null | undefined) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  return ms < 60000 ? `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s` : `${Math.floor(ms / 60000)} m ${Math.round((ms % 60000) / 1000)} s`;
}

export function tokens(n: number | null | undefined) {
  if (!n) return "0";
  return n < 1000 ? String(n) : `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
}

export function SpanIcon({ span, size = 28 }: { span: Pick<Span, "kind" | "name" | "status">; size?: number }) {
  return (
    <span className={`span-icon kind-${span.kind}${span.status === "error" ? " is-error" : ""}`}
          style={{ width: size, height: size }} title={KIND_LABEL[span.kind] ?? span.kind}>
      <Icon name={spanIcon(span)} size={Math.round(size * 0.55)} />
    </span>
  );
}

function depthOf(span: Span, byId: Map<string, Span>) {
  let depth = 0;
  for (let p = span.parent_id; p && byId.has(p); p = byId.get(p)!.parent_id) depth += 1;
  return depth;
}

export function SpanTree({ run, spans, selected, onSelect }: {
  run: AgentRun; spans: Span[]; selected: string | null; onSelect: (id: string) => void;
}) {
  const byId = new Map(spans.map((s) => [s.span_id, s]));
  const t0 = new Date(run.started_at).getTime();
  const total = Math.max(1, run.duration_ms ?? Math.max(...spans.map((s) => new Date(s.started_at).getTime() - t0 + (s.duration_ms ?? 0))));
  return (
    <div className="span-tree" role="listbox" aria-label="Steps">
      {spans.map((s) => {
        const offset = ((new Date(s.started_at).getTime() - t0) / total) * 100;
        const width = Math.max(0.6, ((s.duration_ms ?? 0) / total) * 100);
        const llmTokens = (Number(s.attributes["gen_ai.usage.input_tokens"]) || 0) + (Number(s.attributes["gen_ai.usage.output_tokens"]) || 0);
        return (
          <button key={s.span_id} role="option" aria-selected={selected === s.span_id}
                  className={`span-row${selected === s.span_id ? " selected" : ""}`} onClick={() => onSelect(s.span_id)}>
            <span className="span-main" style={{ paddingLeft: depthOf(s, byId) * 18 }}>
              <SpanIcon span={s} />
              <span className="span-name">
                <span className="span-title">{s.name}</span>
                <span className="span-meta">
                  {KIND_LABEL[s.kind] ?? s.kind}
                  {s.kind === "llm" && llmTokens > 0 && <> · {tokens(llmTokens)} tok</>}
                  {s.attributes["gaugelens.cache_hit"] === true && <> · cached</>}
                  {s.status === "error" && <span className="span-error"> · {String(s.attributes["error.message"] ?? "error")}</span>}
                </span>
              </span>
            </span>
            <span className="span-bar" aria-hidden><span className={`kind-${s.kind}`} style={{ left: `${offset}%`, width: `${Math.min(width, 100 - offset)}%` }} /></span>
            <span className="span-ms mono">{duration(s.duration_ms)}</span>
          </button>
        );
      })}
    </div>
  );
}

const isPlain = (v: unknown) => v == null || ["string", "number", "boolean"].includes(typeof v);
const isRecords = (v: unknown): v is Record<string, unknown>[] =>
  Array.isArray(v) && v.length > 0 && v.every((x) => x && typeof x === "object" && !Array.isArray(x));
const EVIDENCE_ID = /^(reading|check|frame|anomaly):[^:]+_([A-Za-z0-9]+)$/;

/** Short, readable form of one value: rounded numbers, evidence IDs as kind:…last 8 (full ID on hover). */
function plain(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Math.round(v * 1000) / 1000);
  if (typeof v === "string") {
    const m = v.match(EVIDENCE_ID);
    return m ? `${m[1]}:…${m[2].slice(-8)}` : v;
  }
  return String(v);
}
const cell = (v: unknown) => (Array.isArray(v) ? v.map((x) => (isPlain(x) ? plain(x) : JSON.stringify(x))).join(", ") : isPlain(v) ? plain(v) : JSON.stringify(v));
const full = (v: unknown) => (Array.isArray(v) ? v.map(String).join(", ") : typeof v === "string" ? v : undefined);

function RecordsTable({ rows }: { rows: Record<string, unknown>[] }) {
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  return (
    <div className="data-table">
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>{rows.map((row, i) => <tr key={i}>{cols.map((c) => <td key={c} className="mono small" title={full(row[c])}>{cell(row[c])}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

/** Renders tool / model data readably: tables for lists of records, key-value for objects, text for strings. */
export function DataView({ value, depth = 0 }: { value: unknown; depth?: number }): ReactNode {
  if (value == null) return <span className="muted">—</span>;
  if (typeof value === "string" && (depth === 0 || value.length > 120 || value.includes("\n")))
    return <pre className="data-text">{value}</pre>;
  if (isPlain(value)) return <span className="mono" title={full(value)}>{plain(value)}</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="muted">(empty)</span>;
    if (isRecords(value)) return <RecordsTable rows={value} />;
    return <ul className="data-list">{value.map((v, i) => <li key={i}>{isPlain(v) ? String(v) : <DataView value={v} depth={depth + 1} />}</li>)}</ul>;
  }
  if (depth > 3) return <pre className="data-text">{JSON.stringify(value, null, 2)}</pre>;
  const entries = Object.entries(value as Record<string, unknown>);
  const tables = entries.filter(([, v]) => isRecords(v));                // full width, titled, below the scalars
  const rest = entries.filter(([, v]) => !isRecords(v));
  return (
    <div className="stack" style={{ gap: 12 }}>
      {rest.length > 0 && (
        <dl className="data-kv">
          {rest.map(([k, v]) => <div key={k} className="data-kv-row"><dt>{k}</dt><dd><DataView value={v} depth={depth + 1} /></dd></div>)}
        </dl>
      )}
      {tables.map(([k, v]) => (
        <div key={k}><div className="data-block-title">{k} <span className="muted">({(v as unknown[]).length})</span></div><RecordsTable rows={v as Record<string, unknown>[]} /></div>
      ))}
    </div>
  );
}

type Message = { role: string; content: string; tool_calls?: { name: string; arguments: string }[]; tool_call_id?: string };

function Messages({ messages }: { messages: Message[] }) {
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? messages : messages.slice(-4);
  return (
    <div className="messages">
      {!showAll && messages.length > shown.length && (
        <button className="btn small-btn" onClick={() => setShowAll(true)}>Show all {messages.length} messages</button>
      )}
      {shown.map((m, i) => (
        <div key={i} className={`msg role-${m.role}`}>
          <div className="msg-role">{m.role}{m.tool_call_id ? " · tool result" : ""}</div>
          {m.content && <pre className="data-text">{m.content}</pre>}
          {m.tool_calls?.map((c, j) => <div key={j} className="msg-call mono small"><Icon name="tool" size={12} /> {c.name}({c.arguments})</div>)}
        </div>
      ))}
    </div>
  );
}

function isMessages(v: unknown): v is Message[] {
  return Array.isArray(v) && v.length > 0 && v.every((m) => m && typeof m === "object" && "role" in (m as object));
}

function LlmOutput({ value }: { value: { content?: string | null; tool_calls?: { name: string; arguments: unknown }[] } }) {
  return (
    <div className="stack" style={{ gap: 10 }}>
      {value.content && <pre className="data-text">{value.content}</pre>}
      {value.tool_calls?.map((c, i) => (
        <div key={i} className="msg-call"><Icon name="tool" size={12} /> <strong className="mono">{c.name}</strong> <DataView value={c.arguments} /></div>
      ))}
      {!value.content && !value.tool_calls?.length && <span className="muted">(no text)</span>}
    </div>
  );
}

export function SpanDetail({ span }: { span: Span }) {
  const [tab, setTab] = useState<"output" | "input" | "attributes">("output");
  const image = span.attributes["gaugelens.image_uri"] as string | undefined;
  const isLlmOutput = span.kind === "llm" && span.output && typeof span.output === "object" && "tool_calls" in (span.output as object);
  return (
    <div className="span-detail">
      <div className="span-detail-head">
        <SpanIcon span={span} size={34} />
        <div style={{ minWidth: 0 }}>
          <h2>{span.name}</h2>
          <div className="row small muted" style={{ gap: 8, marginTop: 2 }}>
            <span>{KIND_LABEL[span.kind] ?? span.kind}</span>·<span className="mono">{duration(span.duration_ms)}</span>
            {span.status === "error" ? <Badge tone="bad">Error</Badge> : <Badge tone="ok">OK</Badge>}
          </div>
        </div>
      </div>
      {span.status === "error" && <div className="error" style={{ margin: "0 16px 12px" }}>{String(span.attributes["error.message"] ?? "error")}</div>}
      {image && <img className="span-image" src={evidenceUrl(image)} alt={`frame viewed by the agent`} />}
      <div className="tabs" role="tablist">
        {(["output", "input", "attributes"] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      <div className="tab-body">
        {tab === "input" && (isMessages(span.input) ? <Messages messages={span.input} /> : <DataView value={span.input} />)}
        {tab === "output" && (isLlmOutput ? <LlmOutput value={span.output as never} /> : <DataView value={span.output} />)}
        {tab === "attributes" && (
          <dl className="data-kv">
            {Object.entries(span.attributes).map(([k, v]) => (
              <div key={k} className="data-kv-row"><dt className="mono">{k}</dt><dd className="mono">{cell(v)}</dd></div>
            ))}
            <div className="data-kv-row"><dt className="mono">span_id</dt><dd className="mono">{span.span_id}</dd></div>
            <div className="data-kv-row"><dt className="mono">started_at</dt><dd className="mono">{span.started_at}</dd></div>
          </dl>
        )}
      </div>
    </div>
  );
}
