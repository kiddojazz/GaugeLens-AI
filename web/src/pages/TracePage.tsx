import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorBox, Loading, Status } from "../components";
import { ruleLabel, shortId, timeAgo } from "../format";
import { useApi } from "../hooks";
import { SpanDetail, SpanTree, duration, tokens } from "../trace";

export default function TracePage() {
  const { runId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const { data, error } = useApi(() => api.trace(runId), [runId]);
  const [selected, setSelected] = useState<string | null>(params.get("span"));

  useEffect(() => {                                  // default: the first step after the root
    if (data && !selected) setSelected(data.spans[1]?.span_id ?? data.spans[0]?.span_id ?? null);
  }, [data, selected]);

  if (error) return <ErrorBox error={error} />;
  if (!data) return <Loading />;
  const { run, spans } = data;
  const span = spans.find((s) => s.span_id === selected) ?? spans[0];

  function select(id: string) {
    setSelected(id);
    setParams({ span: id }, { replace: true });
  }

  return (
    <>
      <div className="page-head">
        <div style={{ minWidth: 0 }}>
          <div className="muted small">
            <Link to="/traces">Traces</Link> / {shortId(run.run_id)}
            {run.eval_run_id && <> · <Link to={`/evals?run=${run.eval_run_id}`}>evaluation {shortId(run.eval_run_id)}</Link></>}
          </div>
          <h1 className="row" style={{ marginTop: 4 }}>
            {run.scenario_id ? `Eval: ${run.scenario_id.replace(/_/g, " ")}` : ruleLabel(run.rule_id ?? "diagnosis")}
            <Status value={run.status === "ok" ? (run.verified ? "verified" : "needs_review") : run.status} />
          </h1>
          <p>{run.headline ?? "—"}</p>
        </div>
        {run.anomaly_id && !run.eval_run_id && <Link className="btn" to={`/anomalies/${run.anomaly_id}`}>Open anomaly</Link>}
      </div>

      <div className="trace-stats">
        <div><span>Started</span><strong>{timeAgo(run.started_at)}</strong></div>
        <div><span>Duration</span><strong className="mono">{duration(run.duration_ms)}</strong></div>
        <div><span>Model calls</span><strong className="mono">{run.llm_calls}</strong></div>
        <div><span>Tool calls</span><strong className="mono">{run.tool_calls}</strong></div>
        <div><span>Tokens in / out</span><strong className="mono">{tokens(run.input_tokens)} / {tokens(run.output_tokens)}</strong></div>
        <div><span>Model</span><strong className="mono">{run.model ?? "—"}</strong></div>
        <div><span>Prompt</span><strong className="mono">{run.prompt_version ?? "—"}</strong></div>
      </div>
      {run.error && <ErrorBox error={run.error} />}

      <div className="trace-grid">
        <section className="card">
          <div className="card-head"><h2>Steps</h2><Badge>{spans.length} spans</Badge></div>
          <SpanTree run={run} spans={spans} selected={span?.span_id ?? null} onSelect={select} />
        </section>
        <section className="card trace-detail">{span ? <SpanDetail key={span.span_id} span={span} /> : null}</section>
      </div>
    </>
  );
}
