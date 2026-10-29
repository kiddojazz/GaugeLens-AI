import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type EvalResult } from "../api";
import { Badge, Card, Empty, ErrorBox, Loading, Progress, Status } from "../components";
import { shortId, timeAgo } from "../format";
import { useApi, usePolling } from "../hooks";
import { Icon } from "../icons";
import { duration, tokens } from "../trace";

const CRITERIA: Record<string, string> = {
  grounded: "Every cited ID and number exists",
  right_cause: "Most likely cause is right",
  cites_physics: "Cites the failing physics rule",
  safe_action: "Proposed action is safe",
  labels_simulated: "Says the data is simulated",
  resists_injection: "Ignores the planted instruction",
  completed: "Scenario ran",
};

function Criteria({ result }: { result: EvalResult }) {
  return (
    <ul className="criteria">
      {Object.entries(result.criteria).map(([name, c]) => (
        <li key={name} className={c.pass ? "pass" : "fail"} title={c.detail}>
          <Icon name={c.pass ? "check" : "cross"} size={13} /> {CRITERIA[name] ?? name}
          {!c.pass && <div className="criteria-detail">{c.detail}</div>}
        </li>
      ))}
    </ul>
  );
}

export default function EvalsPage() {
  const [params, setParams] = useSearchParams();
  const runs = useApi(() => api.evals(), []);
  const scenarios = useApi(() => api.scenarios(), []);
  const selectedId = params.get("run") ?? runs.data?.[0]?.eval_run_id ?? null;
  const detail = useApi(() => (selectedId ? api.evalRun(selectedId) : Promise.resolve(null)), [selectedId]);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const active = (runs.data ?? []).some((r) => r.status === "queued" || r.status === "running");
  usePolling(() => { void runs.reload(); void detail.reload(); }, 3000, active);

  async function start() {
    if (!window.confirm("Run all evaluation scenarios now? Each one calls the model several times (uses API credits).")) return;
    setStarting(true);
    setError(null);
    try {
      const { eval_run_id } = await api.startEval();
      setParams({ run: eval_run_id });
      await runs.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  }

  const run = detail.data?.run;
  const results = detail.data?.results ?? [];
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Evaluations</h1>
          <p>Fixed fault scenarios with known answers, run through the real diagnosis agent and scored by rules, not by another model.</p>
        </div>
        <button className="btn primary" onClick={start} disabled={starting || active}>
          <Icon name="play" size={13} /> {active ? "Evaluation running…" : "Run evaluation"}
        </button>
      </div>
      <ErrorBox error={error ?? runs.error} />

      {run && (
        <div className="grid-stats">
          <div className="card stat">
            <div className="stat-value">{run.score != null ? `${Math.round(run.score * 100)}%` : "—"}</div>
            <div className="stat-label">Criteria met</div>
          </div>
          <div className="card stat">
            <div className="stat-value">{run.passed} / {run.scenarios}</div>
            <div className="stat-label">Scenarios fully passed</div>
          </div>
          <div className="card stat">
            <div className="stat-value mono" style={{ fontSize: 18, paddingTop: 6 }}>{run.model ?? "—"}</div>
            <div className="stat-label">Model · prompt {run.prompt_version ?? "—"}</div>
          </div>
        </div>
      )}

      <div className="stack">
        {run && (
          <Card title={<div><h2>Results</h2><div className="muted small">evaluation {shortId(run.eval_run_id)} · {timeAgo(run.started_at)}</div></div>}
                actions={run.status === "done" ? <Status value="done" /> : run.status === "failed" ? <Status value="failed" /> :
                  <div className="row"><Progress value={run.progress ?? 0} /><span className="small muted">{run.status}</span></div>}
                pad={false}>
            {run.error && <ErrorBox error={run.error} />}
            {!results.length ? <Empty>{run.status === "done" ? "No results." : "Scenarios are running…"}</Empty> : (
              <table>
                <thead><tr><th>Scenario</th><th>Criteria</th><th className="num">Score</th><th className="num">Steps</th><th className="num">Time</th><th /></tr></thead>
                <tbody>
                  {results.map((r) => (
                    <tr key={r.scenario_id}>
                      <td className="finding">
                        <div className="row" style={{ gap: 8 }}><strong>{r.title}</strong>{r.passed ? <Badge tone="ok">Pass</Badge> : <Badge tone="bad">Fail</Badge>}</div>
                        {r.headline && <div className="muted small" style={{ marginTop: 4 }}>{r.headline}</div>}
                      </td>
                      <td><Criteria result={r} /></td>
                      <td className="num">{Math.round(r.score * 100)}%</td>
                      <td className="num">{r.llm_calls != null ? r.llm_calls + (r.tool_calls ?? 0) : "—"}<div className="muted small">{tokens((r.input_tokens ?? 0) + (r.output_tokens ?? 0))} tok</div></td>
                      <td className="num">{duration(r.duration_ms)}</td>
                      <td>{r.run_id && <Link className="btn" to={`/traces/${r.run_id}`}>Trace <Icon name="arrow" size={12} /></Link>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        )}

        <div className="grid-2">
          <Card title="Scenarios" pad={false}>
            {scenarios.loading && !scenarios.data ? <Loading /> : (
              <ul className="list" style={{ padding: "0 16px" }}>
                {(scenarios.data ?? []).map((s) => (
                  <li key={s.id}>
                    <div className="row" style={{ gap: 8 }}><strong>{s.title}</strong>{s.injection && <span className="tag">security</span>}</div>
                    <div className="small muted" style={{ marginTop: 4 }}>{s.description}</div>
                    <div className="small" style={{ marginTop: 4 }}>Expected: {s.expected_cause} · <span className="mono muted">{s.rule_id}</span></div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="History" pad={false}>
            {runs.loading && !runs.data ? <Loading /> : !runs.data?.length ? <Empty>No evaluations yet.</Empty> : (
              <table>
                <thead><tr><th>Run</th><th className="num">Passed</th><th className="num">Score</th><th>When</th></tr></thead>
                <tbody>
                  {runs.data.map((r) => (
                    <tr key={r.eval_run_id} className={`clickable${r.eval_run_id === selectedId ? " selected-row" : ""}`}
                        onClick={() => setParams({ run: r.eval_run_id })}>
                      <td className="mono">{shortId(r.eval_run_id)}<div className="muted small">{r.model}</div></td>
                      <td className="num">{r.passed} / {r.scenarios}</td>
                      <td className="num">{r.score != null ? `${Math.round(r.score * 100)}%` : r.status}</td>
                      <td className="muted">{timeAgo(r.started_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
