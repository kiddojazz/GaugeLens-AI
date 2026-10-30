import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api, type Diagnosis } from "../api";
import { ChatPanel } from "../chat";
import { Badge, Card, Empty, ErrorBox, Loading, Simulated, Status } from "../components";
import { pct, ruleLabel, shortId, timeAgo } from "../format";
import { useApi, usePolling } from "../hooks";
import { Icon } from "../icons";
import { SpanIcon, duration } from "../trace";

const LIKELIHOOD_TONE = { high: "bad", medium: "warn", low: "neutral" } as const;

export default function AnomalyPage() {
  const { anomalyId = "" } = useParams();
  const navigate = useNavigate();
  const anomaly = useApi(() => api.anomaly(anomalyId), [anomalyId]);
  const diagnosis = useApi<Diagnosis | null>(
    () => api.diagnosis(anomalyId).catch((e) => (e instanceof ApiError && e.status === 404 ? null : Promise.reject(e))),
    [anomalyId],
  );
  const clipId = anomaly.data?.clip_id;
  const readings = useApi(() => (clipId ? api.readings(clipId) : Promise.resolve([])), [clipId]);
  const checks = useApi(() => (clipId ? api.checks(clipId) : Promise.resolve([])), [clipId]);
  const traces = useApi(() => api.traces({ anomaly_id: anomalyId }), [anomalyId, diagnosis.data?.diagnosis_id]);
  const runId = diagnosis.data?.output.run_id ?? traces.data?.[0]?.run_id;
  const trace = useApi(() => (runId ? api.trace(runId) : Promise.resolve(null)), [runId]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const waiting = !diagnosis.data && (anomaly.data?.diagnose_job === "queued" || anomaly.data?.diagnose_job === "running");
  usePolling(() => { void diagnosis.reload(); void anomaly.reload(); }, 3000, waiting);

  // evidence id -> the frame it belongs to, so every citation can open the footage at that moment
  const frameOf = useMemo(() => {
    const map = new Map<string, string>();
    for (const r of readings.data ?? []) map.set(`reading:${r.reading_id}`, r.frame_id);
    for (const c of checks.data ?? []) if (c.frame_id) map.set(`check:${c.check_id}`, c.frame_id);
    return map;
  }, [readings.data, checks.data]);

  function openCitation(ref: string) {
    const frame = ref.startsWith("frame:") ? ref.slice(6) : frameOf.get(ref);
    if (clipId) navigate(`/clips/${clipId}${frame ? `?frame=${frame}` : ""}`);
  }

  const Cite = ({ refId }: { refId: string }) =>
    /^(reading|check|frame|anomaly):/.test(refId)
      ? <button className="cite" onClick={() => openCitation(refId)} title={refId}>{refId.split(":")[0]} {shortId(refId.split(":")[1])}</button>
      : <span className="muted small">{refId}</span>;

  async function run() {
    setBusy(true);
    setError(null);
    try {
      await api.diagnose(anomalyId);
      await anomaly.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function decide(decision: "approved" | "rejected") {
    if (!diagnosis.data) return;
    setBusy(true);
    setError(null);
    try {
      await api.decide(diagnosis.data.diagnosis_id, decision, note);
      await Promise.all([diagnosis.reload(), anomaly.reload()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (anomaly.error) return <ErrorBox error={anomaly.error} />;
  if (!anomaly.data) return <Loading />;
  const a = anomaly.data;
  const d = diagnosis.data;
  const o = d?.output;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="muted small"><Link to="/incidents">Incidents</Link> / anomaly {shortId(a.anomaly_id)}</div>
          <h1 className="row" style={{ marginTop: 4 }}>{ruleLabel(a.rule_id)} <Status value={a.severity} /> {a.is_simulated && <Simulated />}</h1>
          <p>{a.equipment_id}{a.equipment_model ? ` · ${a.equipment_model}` : ""} · detected {timeAgo(a.ts_start)} · <Link to={`/clips/${a.clip_id}`}>open clip</Link></p>
        </div>
      </div>
      <ErrorBox error={error ?? diagnosis.error} />

      <div className="grid-2">
        <div className="stack">
          {!d ? (
            <Card title="Diagnosis">
              {waiting ? <Empty>The diagnosis agent is investigating… (Muse Spark, read-only tools)</Empty> : (
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="muted">No diagnosis yet.</span>
                  <button className="btn primary" onClick={run} disabled={busy}>Run diagnosis</button>
                </div>
              )}
            </Card>
          ) : (
            <>
              <Card title={<div><h2 style={{ fontSize: 17 }}>{o!.headline}</h2><div className="muted small" style={{ marginTop: 4 }}>{d.verified ? `Evidence-checked · ${d.model}` : "Could not be verified automatically; showing stored facts only"}</div></div>}
                    actions={<div className="row">{d.verified ? <Badge tone="ok">Evidence-checked</Badge> : <Badge tone="warn">Needs expert review</Badge>}<Badge>Confidence: {o!.confidence}</Badge></div>}>
                <h3 style={{ marginBottom: 6 }}>Observations</h3>
                <ul className="list">
                  {o!.observations.map((obs, i) => (
                    <li key={i}>{obs.statement}<div className="row" style={{ gap: 6, marginTop: 4 }}>{obs.evidence.map((e) => <Cite key={e} refId={e} />)}</div></li>
                  ))}
                </ul>
              </Card>

              <Card title="Physics" pad={false}>
                {!o!.physics.length ? <Empty>None cited.</Empty> : (
                  <table>
                    <thead><tr><th>Rule</th><th>Result</th><th className="num">Residual</th><th>Evidence</th></tr></thead>
                    <tbody>
                      {o!.physics.map((p, i) => (
                        <tr key={i}><td>{ruleLabel(p.rule_id)}</td><td><Status value={p.result} /></td><td className="num">{pct(p.residual_pct)}</td>
                          <td><div className="row" style={{ gap: 6 }}>{p.evidence.map((e) => <Cite key={e} refId={e} />)}</div></td></tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>

              <Card title="Possible causes">
                <ul className="list">
                  {o!.hypotheses.map((h, i) => (
                    <li key={i}>
                      <div className="row" style={{ justifyContent: "space-between" }}><span>{h.cause}</span><Badge tone={LIKELIHOOD_TONE[h.likelihood]}>{h.likelihood}</Badge></div>
                      {(h.supporting.length > 0 || h.against.length > 0) && (
                        <div className="small muted" style={{ marginTop: 4 }}>
                          {h.supporting.length > 0 && <div className="row" style={{ gap: 6 }}>For: {h.supporting.map((s) => <Cite key={s} refId={s} />)}</div>}
                          {h.against.length > 0 && <div className="row" style={{ gap: 6 }}>Against: {h.against.map((s) => <Cite key={s} refId={s} />)}</div>}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
                {o!.unknowns.length > 0 && (
                  <>
                    <h3 style={{ margin: "14px 0 6px" }}>What the evidence can't tell</h3>
                    <ul style={{ margin: 0, paddingLeft: 18 }} className="muted">{o!.unknowns.map((u, i) => <li key={i}>{u}</li>)}</ul>
                  </>
                )}
              </Card>
            </>
          )}
        </div>

        <div className="stack">
          {o && (
            <Card title="Proposed next step">
              <div className="stack">
                <div className="notice action">{o.proposed_action}</div>
                <div className="notice safety"><strong>Safety:</strong> {o.safety_note}</div>
              </div>
            </Card>
          )}
          {d && (
            <Card title="Decision">
              {d.decision === "pending" ? (
                <div className="stack">
                  <label className="field">Note (optional)<textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Technician will check on Friday" /></label>
                  <div className="row">
                    <button className="btn primary" onClick={() => decide("approved")} disabled={busy}>Approve</button>
                    <button className="btn danger" onClick={() => decide("rejected")} disabled={busy}>Reject</button>
                  </div>
                  <span className="muted small">Approving records the decision. Nothing acts on the equipment.</span>
                </div>
              ) : (
                <dl className="kv">
                  <dt>Decision</dt><dd><Status value={d.decision} /></dd>
                  <dt>By</dt><dd>{d.decided_by}</dd>
                  <dt>When</dt><dd>{timeAgo(d.decided_at)}</dd>
                  {d.decision_note && <><dt>Note</dt><dd>{d.decision_note}</dd></>}
                </dl>
              )}
            </Card>
          )}
          {a.clip_id && (
            <ChatPanel clipId={a.clip_id} anomalyId={a.anomaly_id} onCite={openCitation}
                       suggestions={d ? ["Why this cause and not another?", "What would confirm this diagnosis?",
                                         "What happened before the fault started?"]
                                      : ["What does this anomaly mean?", "Which readings broke the rule?"]} />
          )}
          <Card title="Evidence">
            <dl className="kv small">
              <dt>Checks</dt><dd className="row" style={{ gap: 6 }}>{(a.evidence?.check_ids ?? []).slice(0, 6).map((c) => <Cite key={c} refId={`check:${c}`} />)}</dd>
              <dt>Frames</dt><dd className="row" style={{ gap: 6 }}>{(a.evidence?.frame_ids ?? []).slice(0, 6).map((f) => <Cite key={f} refId={`frame:${f}`} />)}</dd>
            </dl>
          </Card>
          {trace.data ? (
            <Card title="How the agent investigated" pad={false}
                  actions={<Link className="btn" to={`/traces/${trace.data.run.run_id}`}>Full trace <Icon name="arrow" size={12} /></Link>}>
              <ol className="mini-trace">
                {trace.data.spans.slice(1).map((sp) => (
                  <li key={sp.span_id}>
                    <Link to={`/traces/${trace.data!.run.run_id}?span=${sp.span_id}`}>
                      <SpanIcon span={sp} size={24} />
                      <span className="mini-trace-name">{sp.name}{sp.kind === "tool" && !!sp.input && typeof sp.input === "object" && Object.keys(sp.input).length > 0 &&
                        <span className="muted mono small"> {Object.values(sp.input as Record<string, unknown>).map(String).join(", ")}</span>}</span>
                      <span className="muted mono small">{duration(sp.duration_ms)}</span>
                    </Link>
                  </li>
                ))}
              </ol>
            </Card>
          ) : o?.trace && o.trace.length > 0 && (
            <Card title="How the agent investigated">
              <details>
                <summary>{o.trace.length} tool calls</summary>
                <ol className="small" style={{ paddingLeft: 18, marginBottom: 0 }}>
                  {o.trace.map((t, i) => <li key={i} style={{ marginTop: 6, overflowWrap: "anywhere" }}><span className="mono">{t.tool}</span> <span className="muted mono">{t.arguments}</span></li>)}
                </ol>
                <p className="muted small" style={{ marginBottom: 0 }}>This diagnosis predates step-by-step tracing.</p>
              </details>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
