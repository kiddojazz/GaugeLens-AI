// Admin: who is using GaugeLens. Accounts, live guest sessions (their data is deleted when they end), and how much
// guest data was purged recently. Clips themselves are on the Clips page, where admins see everyone's.
import { api } from "../api";
import { Card, Empty, ErrorBox, Loading } from "../components";
import { shortId, timeAgo } from "../format";
import { useApi, usePolling } from "../hooks";

export default function PeoplePage() {
  const { data, error, loading, reload } = useApi(() => api.people(), []);
  usePolling(reload, 15000, true);
  const unowned = (data?.unowned ?? []).reduce((n, u) => n + u.clips, 0);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>People</h1>
          <p>Accounts and live guest sessions. Guest data is deleted when the session ends; accounts keep theirs.</p>
        </div>
      </div>
      <ErrorBox error={error} />
      {error ? null : loading && !data ? <Loading /> : data && (
        <>
          <div className="grid-stats">
            <div className="card stat"><div className="stat-value">{data.users.length}</div><div className="stat-label">Accounts</div></div>
            <div className="card stat"><div className="stat-value">{data.guests.length}</div><div className="stat-label">Guests online now</div></div>
            <div className="card stat">
              <div className="stat-value">{data.purged_last_7_days.sessions}</div>
              <div className="stat-label">Guest sessions deleted, last 7 days{data.purged_last_7_days.last ? ` · latest ${timeAgo(data.purged_last_7_days.last)}` : ""}</div>
            </div>
          </div>
          <div className="stack">
            <Card title="Accounts" pad={false}>
              {!data.users.length ? <Empty>No accounts yet.</Empty> : (
                <table>
                  <thead><tr><th>Email</th><th>Role</th><th className="num">Clips</th><th className="num">Questions</th><th>Last seen</th><th>Joined</th></tr></thead>
                  <tbody>
                    {data.users.map((u) => (
                      <tr key={u.user_id}>
                        <td>{u.email}</td>
                        <td>{u.role === "admin" ? <span className="badge admin">Admin</span> : <span className="muted">User</span>}</td>
                        <td className="num">{u.clips}</td>
                        <td className="num">{u.questions}</td>
                        <td className="muted">{u.last_seen_at ? timeAgo(u.last_seen_at) : "never"}</td>
                        <td className="muted">{timeAgo(u.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
            <Card title="Guests online" pad={false}>
              {!data.guests.length ? <Empty>No guests right now.</Empty> : (
                <table>
                  <thead><tr><th>Session</th><th className="num">Clips</th><th className="num">Questions</th><th>Last active</th><th>Started</th></tr></thead>
                  <tbody>
                    {data.guests.map((g) => (
                      <tr key={g.session_id}>
                        <td className="mono">{shortId(g.session_id)}</td>
                        <td className="num">{g.clips}</td>
                        <td className="num">{g.questions}</td>
                        <td className="muted">{timeAgo(g.last_seen_at)}</td>
                        <td className="muted">{timeAgo(g.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <p className="muted small" style={{ padding: "0 16px" }}>A guest's clips and chat are deleted when they refresh or close the page, or after {data.idle_minutes} minutes idle.</p>
            </Card>
            {unowned > 0 && (
              <Card title="Not tied to a person">
                <p style={{ margin: 0 }} className="muted">
                  {data.unowned.map((u) => `${u.clips} from ${u.source_channel}`).join(" · ")}. Only administrators see these
                  (seeded demo data and clips from before accounts existed).
                </p>
              </Card>
            )}
          </div>
        </>
      )}
    </>
  );
}
