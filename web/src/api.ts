// Typed client for the GaugeLens API (/api/v1). Same origin: Vite proxies in dev, FastAPI serves the build.

const BASE = "/api/v1";

export type Health = {
  status: string;
  db: string;
  model: { provider: string; model: string; cache_only: boolean; configured: boolean };
  worker: string;
};

export type Clip = {
  clip_id: string;
  equipment_id: string;
  equipment_kind?: string;
  status: "queued" | "processing" | "done" | "failed";
  source_channel: string;
  is_demo_replay: boolean;
  created_at: string;
  frames?: number;
  anomalies?: number;
  corrections?: number;
  progress?: number | null;
};

export type ClipDetail = {
  clip: Clip & { recorded_at: string; source_uri: string };
  job: { job_id: string; status: string; attempts: number; progress: number; last_error: string | null } | null;
  counts: { frames: number; readings: number };
};

export type FrameBox = { display_id: string; label: string; box: [number, number, number, number]; source: string; tracked: boolean };
export type Frame = { frame_id: string; index: number; t: number; image_uri: string; boxes: FrameBox[] };
export type Frames = { clip_id: string; frame_size: { w: number; h: number } | null; frames: Frame[] };

export type Reading = {
  ts: string;
  reading_id: string;
  instrument_id: string;
  physics_role: string;
  frame_id: string;
  t_offset_s: number;
  value: number | null;
  unit: string | null;
  raw_text: string | null;
  display_page: string | null;
  status: "pending" | "accepted" | "rejected" | "needs_review" | "corrected";
  attempt: number;
  supersedes: string | null;
  reused_from: string | null;
  crop_uri: string | null;
  confidence: number | null;
};

export type RelookAttempt = {
  attempt: number;
  strategy: string;
  value: number | null;
  raw_text: string | null;
  unit: string | null;
  outcome?: string;
  crop_uri: string | null;
  reading_id: string;
  decoder?: string | null;
};

export type Check = {
  check_id: string;
  rule_id: string;
  outcome: "pass" | "misread_fixed" | "persistent_violation" | "needs_review" | "skipped";
  passed: boolean;
  expected: number | null;
  observed: number | null;
  residual_pct: number | null;
  tolerance_used: number | null;
  reason: string | null;
  reading_ids: string[];
  suspect_reading_id: string | null;
  candidate_fix: { role: string; value: number; kind: string } | null;
  relook: { outcome: string; suspect_reading_id: string; attempts: RelookAttempt[] } | null;
  frame_id: string | null;
  t_offset_s: number | null;
};

export type Correction = {
  check_id: string;
  clip_id: string;
  rule_id: string;
  frame_id: string;
  t_offset_s: number;
  role: string;
  unit: string | null;
  original_text: string;
  original_crop: string | null;
  fixed_text: string | null;
  fixed_crop: string | null;
  strategy: string | null;
  ts: string;
};

export type Anomaly = {
  anomaly_id: string;
  ts_start: string;
  ts_end: string | null;
  equipment_id: string;
  equipment_kind?: string;
  equipment_model?: string;
  kind: string;
  severity: "info" | "warning" | "critical";
  summary: string;
  evidence: { check_ids: string[]; reading_ids: string[]; frame_ids: string[] };
  clip_id: string;
  rule_id: string;
  is_simulated: boolean;
  diagnosis_id: string | null;
  verified: boolean | null;
  decision: "pending" | "approved" | "rejected" | null;
  headline: string | null;
  diagnose_job?: string | null;
};

export type DiagnosisOutput = {
  headline: string;
  observations: { statement: string; evidence: string[] }[];
  physics: { rule_id: string; result: string; residual_pct: number | null; evidence: string[] }[];
  hypotheses: { cause: string; likelihood: "low" | "medium" | "high"; supporting: string[]; against: string[] }[];
  unknowns: string[];
  proposed_action: string;
  safety_note: string;
  confidence: "low" | "medium" | "high";
  trace?: { tool: string; arguments: string; result: string }[];
  citation_problems?: string[];
  run_id?: string;
};

export type Diagnosis = {
  diagnosis_id: string;
  anomaly_id: string;
  created_at: string;
  model: string;
  verified: boolean;
  output: DiagnosisOutput;
  decision: "pending" | "approved" | "rejected";
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
};

export type Instrument = { instrument_id: string; physics_role: string; label: string; unit: string; resolution: number | null };
export type Display = { display_id: string; label: string; context: string | null; sam_prompt: string; instruments: Instrument[] };
export type Equipment = {
  equipment_id: string;
  kind: string;
  model: string | null;
  nominal_battery_v: number | null;
  rated_va: number | null;
  idle_w: number | null;
  controller_type: string | null;
  linked_equipment_id: string | null;
  displays: Display[];
};

export type AgentRun = {
  run_id: string;
  kind: string;
  anomaly_id: string | null;
  diagnosis_id: string | null;
  eval_run_id: string | null;
  scenario_id: string | null;
  model: string | null;
  prompt_version: string | null;
  status: "running" | "ok" | "error";
  verified: boolean | null;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  llm_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  error: string | null;
  rule_id: string | null;
  equipment_id: string | null;
  clip_id: string | null;
  headline: string | null;
};

export type SpanKind = "agent" | "context" | "llm" | "tool" | "guardrail" | "fallback";
export type Span = {
  span_id: string;
  parent_id: string | null;
  seq: number;
  name: string;
  kind: SpanKind;
  status: "ok" | "error";
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  attributes: Record<string, unknown>;
  input: unknown;
  output: unknown;
};
export type Trace = { run: AgentRun; spans: Span[] };

export type Criterion = { pass: boolean; detail: string };
export type EvalRun = {
  eval_run_id: string;
  status: "queued" | "running" | "done" | "failed";
  model: string | null;
  prompt_version: string | null;
  scenarios: number;
  passed: number;
  score: number | null;
  started_at: string;
  ended_at: string | null;
  error: string | null;
  progress: number | null;
};
export type EvalResult = {
  scenario_id: string;
  title: string;
  run_id: string | null;
  anomaly_id: string | null;
  diagnosis_id: string | null;
  passed: boolean;
  score: number;
  criteria: Record<string, Criterion>;
  headline: string | null;
  duration_ms: number | null;
  error: string | null;
  tool_calls: number | null;
  llm_calls: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
};
export type Scenario = { id: string; title: string; description: string; rule_id: string; expected_cause: string; injection: boolean };

export type ChatMessage = {
  message_id: string;
  clip_id: string;
  role: "user" | "assistant";
  content: string;
  frame_id: string | null;
  anomaly_id: string | null;
  citations: string[];
  verified: boolean | null;
  problems: string[];
  run_id: string | null;
  created_at: string;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  health: () => request<Health>("/health"),
  clips: () => request<Clip[]>("/clips"),
  clip: (id: string) => request<ClipDetail>(`/clips/${id}`),
  frames: (id: string) => request<Frames>(`/clips/${id}/frames`),
  checks: (id: string) => request<Check[]>(`/clips/${id}/checks`),
  readings: (clipId: string) => request<Reading[]>(`/readings?clip_id=${encodeURIComponent(clipId)}&limit=5000`),
  corrections: (clipId?: string) => request<Correction[]>(`/corrections${clipId ? `?clip_id=${clipId}` : ""}`),
  anomalies: (clipId?: string) => request<Anomaly[]>(`/anomalies${clipId ? `?clip_id=${clipId}` : ""}`),
  anomaly: (id: string) => request<Anomaly>(`/anomalies/${id}`),
  diagnosis: (anomalyId: string) => request<Diagnosis>(`/anomalies/${anomalyId}/diagnosis`),
  diagnose: (anomalyId: string) => request<{ job_id: string }>(`/anomalies/${anomalyId}/diagnose`, { method: "POST" }),
  decide: (diagnosisId: string, decision: "approved" | "rejected", note: string) =>
    request<Diagnosis>(`/diagnoses/${diagnosisId}/decision`, json({ decision, note: note || null, decided_by: "web" })),
  equipment: () => request<Equipment[]>("/equipment"),
  traces: (params: { anomaly_id?: string; eval_run_id?: string } = {}) =>
    request<AgentRun[]>(`/traces?${new URLSearchParams(params as Record<string, string>)}`),
  trace: (runId: string) => request<Trace>(`/traces/${runId}`),
  evals: () => request<EvalRun[]>("/evals"),
  evalRun: (id: string) => request<{ run: EvalRun; results: EvalResult[] }>(`/evals/${id}`),
  scenarios: () => request<Scenario[]>("/evals/scenarios"),
  startEval: () => request<{ eval_run_id: string; job_id: string }>("/evals", json({})),
  chat: (clipId: string) => request<ChatMessage[]>(`/clips/${clipId}/chat`),
  ask: (clipId: string, question: string, frameId?: string | null, anomalyId?: string | null) =>
    request<ChatMessage>(`/clips/${clipId}/chat`, json({ question, frame_id: frameId ?? null, anomaly_id: anomalyId ?? null })),
  clearChat: (clipId: string) => request<void>(`/clips/${clipId}/chat`, { method: "DELETE" }),
  upload: (file: File, equipmentId: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("equipment_id", equipmentId);
    return request<{ clip_id: string; job_id: string }>("/clips", { method: "POST", body: form });
  },
};

export const evidenceUrl = (uri: string) => `${BASE}/evidence?uri=${encodeURIComponent(uri)}`;
export const eventsUrl = (clipId: string) => `${BASE}/clips/${clipId}/events`;
