import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, Empty, ErrorBox, Loading, Progress, Simulated, Status } from "../components";
import { shortId, timeAgo } from "../format";
import { useApi, usePolling } from "../hooks";

export default function ClipsPage() {
  const navigate = useNavigate();
  const { data, error, loading, reload } = useApi(() => api.clips(), []);
  const busy = (data ?? []).some((c) => c.status === "queued" || c.status === "processing");
  usePolling(reload, 3000, busy);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Clips</h1>
          <p>Every video processed: frames read, physics checked, misreads corrected.</p>
        </div>
        <Link className="btn primary" to="/upload">Upload clip</Link>
      </div>
      <Card pad={false}>
        <ErrorBox error={error} />
        {error ? null : loading && !data ? <Loading /> : !data?.length ? <Empty>No clips yet. Upload one to start.</Empty> : (
          <table>
            <thead>
              <tr><th>Clip</th><th>Equipment</th><th>Status</th><th className="num">Frames</th><th className="num">Corrections</th><th className="num">Anomalies</th><th>Source</th><th>Added</th></tr>
            </thead>
            <tbody>
              {data.map((c) => (
                <tr key={c.clip_id} className="clickable" onClick={() => navigate(`/clips/${c.clip_id}`)}>
                  <td><div className="row"><span className="mono">{shortId(c.clip_id)}</span>{c.is_demo_replay && <Simulated />}</div></td>
                  <td>{c.equipment_id}<div className="muted small">{c.equipment_kind}</div></td>
                  <td>
                    {c.status === "processing" && c.progress != null ? (
                      <div className="row"><Progress value={c.progress} /><span className="muted small mono">{Math.round(c.progress * 100)}%</span></div>
                    ) : <Status value={c.status} />}
                  </td>
                  <td className="num">{c.frames}</td>
                  <td className="num">{c.corrections}</td>
                  <td className="num">{c.anomalies}</td>
                  <td className="muted">{c.source_channel}</td>
                  <td className="muted">{timeAgo(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
