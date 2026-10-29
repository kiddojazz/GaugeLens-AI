import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, Empty, ErrorBox, Loading, Status } from "../components";
import { ruleLabel, shortId, timeAgo } from "../format";
import { useApi } from "../hooks";
import { duration, tokens } from "../trace";

export default function TracesPage() {
  const navigate = useNavigate();
  const { data, error, loading } = useApi(() => api.traces(), []);
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Traces</h1>
          <p>Every run of the diagnosis agent, step by step: what it read, which tools it called, and how its answer was checked.</p>
        </div>
      </div>
      <Card pad={false}>
        <ErrorBox error={error} />
        {error ? null : loading && !data ? <Loading /> : !data?.length ? (
          <Empty>No agent runs yet. Runs are recorded when the agent diagnoses an anomaly or an evaluation runs.</Empty>
        ) : (
          <table>
            <thead><tr><th>Run</th><th>Investigated</th><th>Result</th><th className="num">Steps</th><th className="num">Tokens</th><th className="num">Duration</th><th>Started</th></tr></thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.run_id} className="clickable" onClick={() => navigate(`/traces/${r.run_id}`)}>
                  <td className="mono">{shortId(r.run_id)}</td>
                  <td className="finding">
                    <div>{r.headline?.replace(/^SIMULATED( data)?:\s*/i, "") ?? "—"}</div>
                    <div className="row small muted" style={{ marginTop: 4 }}>
                      {ruleLabel(r.rule_id ?? "")}{r.scenario_id && <span className="tag">eval · {r.scenario_id.replace(/_/g, " ")}</span>}
                    </div>
                  </td>
                  <td><Status value={r.status === "ok" ? (r.verified ? "verified" : "needs_review") : r.status} /></td>
                  <td className="num">{r.llm_calls + r.tool_calls}</td>
                  <td className="num">{tokens(r.input_tokens + r.output_tokens)}</td>
                  <td className="num">{duration(r.duration_ms)}</td>
                  <td className="muted">{timeAgo(r.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
