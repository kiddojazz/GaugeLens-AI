import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, evidenceUrl, type Check, type Frame, type Reading } from "../api";
import { ChatPanel } from "../chat";
import { Card, Empty, ErrorBox, Loading, Progress, Simulated, Sparkline, Status } from "../components";
import { pct, roleLabel, ruleLabel, shortId, timeAgo } from "../format";
import { useApi, useClipEvents, usePolling } from "../hooks";

function FrameViewer({ frame, size }: { frame: Frame; size: { w: number; h: number } | null }) {
  const w = size?.w ?? 1;
  const h = size?.h ?? 1;
  return (
    <div className="viewer">
      <img src={evidenceUrl(frame.image_uri)} alt={`Frame at ${frame.t.toFixed(1)} s`} />
      {size && (
        <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden>
          {frame.boxes.map((b) => {
            const [x, y, bw, bh] = b.box;
            return (
              <g key={b.display_id}>
                <rect x={x} y={y} width={bw} height={bh} fill="none" stroke="#7fb0ff" strokeWidth={Math.max(2, w / 500)} />
                <rect x={x} y={Math.max(0, y - h * 0.035)} width={Math.min(bw, w * 0.3)} height={h * 0.035} fill="rgba(15,20,25,0.75)" />
                <text x={x + 6} y={Math.max(0, y - h * 0.035) + h * 0.025} fill="#dbe7ff" fontSize={h * 0.022} fontFamily="system-ui">
                  {b.label}{b.tracked ? " · tracked" : ""}
                </text>
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}

function FrameReadings({ readings }: { readings: Reading[] }) {
  if (!readings.length) return <Empty>No readings on this frame.</Empty>;
  return (
    <table>
      <thead><tr><th>Quantity</th><th className="num">Read</th><th>Status</th></tr></thead>
      <tbody>
        {readings.map((r) => (
          <tr key={r.reading_id}>
            <td>
              {roleLabel(r.physics_role)}
              {r.attempt > 1 && <div className="muted small">re-look, attempt {r.attempt}</div>}
              {r.reused_from && <div className="muted small">unchanged since last read</div>}
            </td>
            <td className="num">{r.raw_text ?? "—"} <span className="muted">{r.unit}</span></td>
            <td><Status value={r.status} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FrameChecks({ checks }: { checks: Check[] }) {
  if (!checks.length) return <Empty>No physics checks ran on this frame.</Empty>;
  return (
    <ul className="list" style={{ padding: "0 16px" }}>
      {checks.map((c) => (
        <li key={c.check_id}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span>{ruleLabel(c.rule_id)}</span>
            <Status value={c.outcome} />
          </div>
          <div className="muted small mono">
            {c.residual_pct != null ? `residual ${pct(c.residual_pct)} · tolerance ${c.tolerance_used?.toFixed(0)} %` : c.reason}
          </div>
        </li>
      ))}
    </ul>
  );
}

export default function ClipPage() {
  const { clipId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const clip = useApi(() => api.clip(clipId), [clipId]);
  const frames = useApi(() => api.frames(clipId), [clipId]);
  const readings = useApi(() => api.readings(clipId), [clipId]);
  const checks = useApi(() => api.checks(clipId), [clipId]);
  const corrections = useApi(() => api.corrections(clipId), [clipId]);
  const anomalies = useApi(() => api.anomalies(clipId), [clipId]);
  const [index, setIndex] = useState(0);
  const navigate = useNavigate();

  const processing = clip.data?.clip.status === "queued" || clip.data?.clip.status === "processing";
  const refreshAll = () => { void frames.reload(); void readings.reload(); void checks.reload(); void corrections.reload(); void anomalies.reload(); };
  const pending = useRef<number | null>(null);
  useClipEvents(clipId, (event) => {
    if (event.type === "clip_done" || event.type === "diagnosis" || event.type === "anomaly") void clip.reload();
    if (pending.current) return;                                  // coalesce bursts of events into one refresh
    pending.current = window.setTimeout(() => { pending.current = null; refreshAll(); }, 800);
  }, processing);
  usePolling(() => void clip.reload(), 2000, processing);

  const frameList = frames.data?.frames ?? [];
  useEffect(() => {                                               // ?frame=<id> (citation links) selects that frame
    const wanted = params.get("frame");
    if (!wanted || !frameList.length) return;
    const i = frameList.findIndex((f) => f.frame_id === wanted);
    if (i >= 0) setIndex(i);
  }, [params, frameList.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const current = frameList[Math.min(index, Math.max(0, frameList.length - 1))];
  const frameReadings = useMemo(
    () => (readings.data ?? []).filter((r) => r.frame_id === current?.frame_id).sort((a, b) => a.physics_role.localeCompare(b.physics_role) || a.attempt - b.attempt),
    [readings.data, current],
  );
  const frameChecks = useMemo(() => (checks.data ?? []).filter((c) => c.frame_id === current?.frame_id), [checks.data, current]);
  const trends = useMemo(() => {
    const byRole = new Map<string, { t: number; v: number; unit: string | null }[]>();
    for (const r of readings.data ?? []) {
      if (r.value == null || r.reused_from || r.status === "rejected" || r.status === "needs_review") continue;
      const list = byRole.get(r.physics_role) ?? [];
      list.push({ t: r.t_offset_s, v: r.value, unit: r.unit });
      byRole.set(r.physics_role, list);
    }
    return [...byRole.entries()].map(([role, pts]) => ({ role, pts: pts.sort((a, b) => a.t - b.t) }));
  }, [readings.data]);

  /** A citation in a chat answer: show that moment in the viewer (or open the anomaly). */
  function cite(ref: string) {
    const [kind, id] = [ref.split(":")[0], ref.slice(ref.indexOf(":") + 1)];
    if (kind === "anomaly") return navigate(`/anomalies/${id}`);
    const frame = kind === "frame" ? id
      : kind === "reading" ? readings.data?.find((r) => r.reading_id === id)?.frame_id
      : checks.data?.find((c) => c.check_id === id)?.frame_id;
    const i = frameList.findIndex((f) => f.frame_id === frame);
    if (i >= 0) {
      select(i);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  function select(i: number) {
    setIndex(i);
    if (frameList[i]) setParams({ frame: frameList[i].frame_id }, { replace: true });
  }

  if (clip.error) return <ErrorBox error={clip.error} />;
  if (!clip.data) return <Loading />;
  const c = clip.data.clip;
  const progress = clip.data.job?.progress ?? 0;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="muted small"><Link to="/clips">Clips</Link> / {shortId(c.clip_id)}</div>
          <h1 className="row" style={{ marginTop: 4 }}>
            {c.equipment_id} <Status value={c.status} /> {c.is_demo_replay && <Simulated />}
          </h1>
          <p>Added {timeAgo(c.created_at)} · {clip.data.counts.frames} frames · {clip.data.counts.readings} readings</p>
        </div>
        {processing && (
          <div className="row"><Progress value={progress} /><span className="mono small muted">{Math.round(progress * 100)}%</span></div>
        )}
      </div>
      {clip.data.job?.last_error && <ErrorBox error={clip.data.job.last_error} />}

      <div className="grid-2">
        <div className="stack">
          <Card title="Footage" actions={current && <span className="mono small muted">t = {current.t.toFixed(1)} s · frame {index + 1} / {frameList.length}</span>}>
            {!current ? (processing ? <Empty>Sampling frames…</Empty> : <Empty>No frames.</Empty>) : (
              <>
                <FrameViewer frame={current} size={frames.data?.frame_size ?? null} />
                <div className="viewer-bar">
                  <button className="btn" onClick={() => select(Math.max(0, index - 1))} disabled={index === 0} aria-label="Previous frame">‹</button>
                  <input type="range" min={0} max={Math.max(0, frameList.length - 1)} value={index} onChange={(e) => select(Number(e.target.value))} aria-label="Frame" />
                  <button className="btn" onClick={() => select(Math.min(frameList.length - 1, index + 1))} disabled={index >= frameList.length - 1} aria-label="Next frame">›</button>
                </div>
              </>
            )}
          </Card>

          <Card title="Corrections" actions={<span className="muted small">first read vs. re-look</span>}>
            {!corrections.data?.length ? <Empty>No misreads needed correcting in this clip.</Empty> : (
              <div className="stack">
                {corrections.data.map((x) => (
                  <div key={x.check_id}>
                    <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
                      <span>{roleLabel(x.role)} at <button className="cite" onClick={() => { const i = frameList.findIndex((f) => f.frame_id === x.frame_id); if (i >= 0) select(i); }}>t = {x.t_offset_s} s</button></span>
                      <span className="muted small">{ruleLabel(x.rule_id)} failed → re-look</span>
                    </div>
                    <div className="crops">
                      <figure className="crop" style={{ margin: 0 }}>
                        {x.original_crop && <img src={evidenceUrl(x.original_crop)} alt="first read" />}
                        <figcaption><span className="muted">First read</span><span className="mono">{x.original_text} {x.unit}</span></figcaption>
                      </figure>
                      <figure className="crop" style={{ margin: 0 }}>
                        {x.fixed_crop && <img src={evidenceUrl(x.fixed_crop)} alt="re-look" />}
                        <figcaption><span className="muted">Re-look ({x.strategy === "sam_digits+decoder" ? "SAM + decoder" : "SAM digits"})</span><strong className="mono">{x.fixed_text} {x.unit}</strong></figcaption>
                      </figure>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card title="Trends" pad={false}>
            {!trends.length ? <Empty>No accepted readings yet.</Empty> : (
              <table>
                <thead><tr><th>Quantity</th><th>Over the clip</th><th className="num">Latest</th></tr></thead>
                <tbody>
                  {trends.map(({ role, pts }) => (
                    <tr key={role}>
                      <td>{roleLabel(role)}</td>
                      <td><Sparkline values={pts.map((p) => p.v)} /></td>
                      <td className="num">{pts[pts.length - 1].v} <span className="muted">{pts[pts.length - 1].unit}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </div>

        <div className="stack">
          <Card title="Readings on this frame" pad={false}><FrameReadings readings={frameReadings} /></Card>
          <ChatPanel clipId={clipId} frameId={current?.frame_id} onCite={cite}
                     frameLabel={current ? `frame ${index + 1} · t = ${current.t.toFixed(1)} s` : undefined}
                     suggestions={[
                       "Summarise this clip",
                       ...(corrections.data?.length ? ["Why was a reading corrected?"] : []),
                       ...(anomalies.data?.length ? ["What is the anomaly about?"] : ["Is anything unusual here?"]),
                       "Are the readings on this frame right?",
                     ]} />
          <Card title="Physics on this frame" pad={false}><FrameChecks checks={frameChecks} /></Card>
          <Card title="Anomalies" pad={false}>
            {!anomalies.data?.length ? <Empty>None in this clip.</Empty> : (
              <ul className="list" style={{ padding: "0 16px" }}>
                {anomalies.data.map((a) => (
                  <li key={a.anomaly_id}>
                    <Link to={`/anomalies/${a.anomaly_id}`}>{ruleLabel(a.rule_id)}</Link>
                    <div className="row small" style={{ marginTop: 4 }}><Status value={a.severity} /><Status value={a.decision ?? (a.diagnosis_id ? "pending" : null)} /></div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
