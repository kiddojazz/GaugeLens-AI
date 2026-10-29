import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, evidenceUrl, type Anomaly, type Correction } from "../api";
import { Card, Empty, ErrorBox, Loading, Simulated, Status } from "../components";
import { roleLabel, ruleLabel, shortId, timeAgo } from "../format";
import { useApi, usePolling } from "../hooks";

/** The agent's headline says what's wrong in words; the "SIMULATED data:" prefix is already shown as a tag. */
function finding(a: Anomaly) {
  return (a.headline ?? a.summary).replace(/^SIMULATED( data)?:\s*/i, "");
}

/** The same misread fixed the same way on several frames of a clip is one story, shown once with a count. */
function groupCorrections(corrections: Correction[]) {
  const groups = new Map<string, { first: Correction; count: number }>();
  for (const c of corrections) {
    const key = [c.clip_id, c.role, c.original_text, c.fixed_text].join("|");
    const group = groups.get(key);
    if (group) group.count += 1;
    else groups.set(key, { first: c, count: 1 });
  }
  return [...groups.values()];
}

export default function IncidentsPage() {
  const navigate = useNavigate();
  const anomalies = useApi(() => api.anomalies(), []);
  const corrections = useApi(() => api.corrections(), []);
  const clips = useApi(() => api.clips(), []);
  usePolling(() => { void anomalies.reload(); void corrections.reload(); }, 15000, true);

  const open = (anomalies.data ?? []).filter((a) => a.decision !== "approved" && a.decision !== "rejected");
  const grouped = useMemo(() => groupCorrections(corrections.data ?? []), [corrections.data]);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Incidents</h1>
          <p>Faults confirmed by physics, and misreads GaugeLens caught and corrected on its own.</p>
        </div>
        <Link className="btn primary" to="/upload">Upload clip</Link>
      </div>

      <div className="grid-stats">
        <div className="card stat"><div className="stat-value">{anomalies.data ? open.length : "—"}</div><div className="stat-label">Anomalies awaiting a decision</div></div>
        <div className="card stat"><div className="stat-value">{corrections.data?.length ?? "—"}</div><div className="stat-label">Misreads caught and corrected</div></div>
        <div className="card stat"><div className="stat-value">{clips.data?.length ?? "—"}</div><div className="stat-label">Clips processed</div></div>
      </div>

      <div className="stack">
        <Card title="Anomalies" pad={false}>
          <ErrorBox error={anomalies.error} />
          {anomalies.error ? null : anomalies.loading && !anomalies.data ? <Loading /> : !anomalies.data?.length ? (
            <Empty>No anomalies. Physics checks only open one when a second look confirms the readings.</Empty>
          ) : (
            <table>
              <thead><tr><th>Finding</th><th>Severity</th><th>Equipment</th><th>Detected</th><th>Diagnosis</th><th>Decision</th></tr></thead>
              <tbody>
                {anomalies.data.map((a) => (
                  <tr key={a.anomaly_id} className="clickable" onClick={() => navigate(`/anomalies/${a.anomaly_id}`)}>
                    <td className="finding">
                      <div>{finding(a)}</div>
                      <div className="row small muted" style={{ marginTop: 4 }}>{ruleLabel(a.rule_id)}{a.is_simulated && <Simulated />}</div>
                    </td>
                    <td><Status value={a.severity} /></td>
                    <td className="muted">{a.equipment_id}</td>
                    <td className="muted">{timeAgo(a.ts_start)}</td>
                    <td>{a.diagnosis_id ? (a.verified ? <Status value="verified" /> : <Status value="needs_review" />) : <span className="muted">Not yet</span>}</td>
                    <td><Status value={a.decision ?? (a.diagnosis_id ? "pending" : null)} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Recent corrections" pad={false}>
          <ErrorBox error={corrections.error} />
          {corrections.error ? null : corrections.loading && !corrections.data ? <Loading /> : !grouped.length ? (
            <Empty>No corrections yet. When a reading breaks physics, GaugeLens looks again and fixes it here.</Empty>
          ) : (
            <table>
              <thead><tr><th>Reading</th><th>First read</th><th>Corrected to</th><th>Evidence</th><th>How</th><th>Clip</th></tr></thead>
              <tbody>
                {grouped.slice(0, 12).map(({ first: c, count }) => (
                  <tr key={c.check_id} className="clickable" onClick={() => navigate(`/clips/${c.clip_id}?frame=${c.frame_id}`)}>
                    <td>{roleLabel(c.role)}{count > 1 && <div className="muted small">{count} frames</div>}</td>
                    <td className="mono">{c.original_text} {c.unit}</td>
                    <td className="mono"><strong>{c.fixed_text}</strong> {c.unit}</td>
                    <td>
                      <div className="row" style={{ gap: 6 }}>
                        {c.original_crop && <img src={evidenceUrl(c.original_crop)} alt="first read" height={28} style={{ borderRadius: 3, background: "#0f1419" }} />}
                        {c.fixed_crop && <img src={evidenceUrl(c.fixed_crop)} alt="re-look" height={28} style={{ borderRadius: 3, background: "#0f1419" }} />}
                      </div>
                    </td>
                    <td className="muted small">{c.strategy === "sam_digits+decoder" ? "SAM + digit decoder" : c.strategy === "sam_digits" ? "SAM digits + re-read" : c.strategy ?? "—"}</td>
                    <td className="mono muted">{shortId(c.clip_id)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </>
  );
}
