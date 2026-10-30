-- GaugeLens schema. Runs on plain PostgreSQL 16; init_db.py adds TimescaleDB hypertables when available.
-- Design notes: docs/02_architecture.md (data model), docs/12_jobs_and_events.md (jobs + events).

CREATE TABLE IF NOT EXISTS sites (
  site_id      TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  location     TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS equipment (
  equipment_id      TEXT PRIMARY KEY,
  site_id           TEXT REFERENCES sites(site_id),
  kind              TEXT NOT NULL CHECK (kind IN ('inverter','charge_controller','generator','meter',
                                                  'pressure_system','clamp_meter')),
  model             TEXT,
  nominal_battery_v NUMERIC,                  -- 12 / 24 / 48
  rated_va          NUMERIC,                  -- inverter nameplate
  idle_w            NUMERIC,                  -- no-load draw (manual or measured)
  controller_type   TEXT CHECK (controller_type IN ('mppt','pwm')),
  gen_governor      TEXT CHECK (gen_governor IN ('mechanical','electronic')),
  linked_equipment_id TEXT REFERENCES equipment(equipment_id),   -- clamp meter -> the inverter it measures
  manual_doc_id     TEXT
);

CREATE TABLE IF NOT EXISTS instruments (
  instrument_id TEXT PRIMARY KEY,
  equipment_id  TEXT NOT NULL REFERENCES equipment(equipment_id),
  kind          TEXT NOT NULL CHECK (kind IN ('analog','digital')),
  label         TEXT,                         -- "Battery voltage"
  quantity      TEXT,                         -- voltage | current | power | frequency | pressure | ...
  unit          TEXT,
  physics_role  TEXT,                         -- battery_v, battery_i, output_w, ...
  page_hint     TEXT,                         -- NULL = always visible
  resolution    NUMERIC,                      -- display step, e.g. 0.1 (V) or 1 (%)
  roi           JSONB,                        -- manual region {x, y, w, h} (fallback / dev default)
  calibration   JSONB,                        -- analog: {min_angle, max_angle, min_value, max_value, center_px}
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One physical display (or gauge face) = one crop = one reader call; it yields several instrument fields.
CREATE TABLE IF NOT EXISTS displays (
  display_id     TEXT PRIMARY KEY,
  equipment_id   TEXT NOT NULL REFERENCES equipment(equipment_id),
  kind           TEXT NOT NULL CHECK (kind IN ('digital','analog')),
  label          TEXT,
  roi            JSONB,                       -- {x, y, w, h} in pixels of roi_frame_size (manual / SAM)
  roi_frame_size JSONB,                       -- {w, h} the roi was drawn on; scaled to each clip's frames
  context        TEXT                         -- equipment description for the reader; NEVER expected values
);
ALTER TABLE displays ADD COLUMN IF NOT EXISTS sam_prompt TEXT NOT NULL DEFAULT 'digital display';
ALTER TABLE displays ADD COLUMN IF NOT EXISTS layout_rank SMALLINT;   -- 0 = leftmost display of this prompt in frame
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS display_id TEXT REFERENCES displays(display_id);
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS match JSONB;   -- {"unit": "V", "page": ["batt"], "label": ["batt"]}

CREATE TABLE IF NOT EXISTS clips (
  clip_id        TEXT PRIMARY KEY,
  equipment_id   TEXT REFERENCES equipment(equipment_id),
  recorded_at    TIMESTAMPTZ,
  source_uri     TEXT NOT NULL,
  source_channel TEXT NOT NULL DEFAULT 'web' CHECK (source_channel IN ('web','seed','replay','eval','imported')),
  fps_sampled    NUMERIC,
  status         TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','done','failed')),
  is_demo_replay BOOLEAN NOT NULL DEFAULT false,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Where each display is in THIS clip (camera framing changes between clips): SAM 3.1 or the manual ROI.
CREATE TABLE IF NOT EXISTS clip_rois (
  clip_id     TEXT NOT NULL REFERENCES clips(clip_id),
  display_id  TEXT NOT NULL REFERENCES displays(display_id),
  roi         JSONB NOT NULL,              -- {x, y, w, h} in this clip's frame pixels
  frame_size  JSONB NOT NULL,              -- {w, h}
  source      TEXT NOT NULL CHECK (source IN ('sam', 'manual')),
  confidence  DOUBLE PRECISION,
  sam_object_id INTEGER,
  model_call_id TEXT,
  PRIMARY KEY (clip_id, display_id)
);
ALTER TABLE clip_rois ADD COLUMN IF NOT EXISTS track JSONB;   -- SAM 3.1 video: {sampled frame index: [x, y, w, h]}

CREATE TABLE IF NOT EXISTS clip_annotations (
  clip_id    TEXT NOT NULL REFERENCES clips(clip_id),
  t_seconds  NUMERIC NOT NULL,
  text       TEXT NOT NULL                    -- "kettle on"
);

CREATE TABLE IF NOT EXISTS frames (
  frame_id     TEXT PRIMARY KEY,
  clip_id      TEXT NOT NULL REFERENCES clips(clip_id),
  t_offset_s   NUMERIC NOT NULL,
  ts           TIMESTAMPTZ NOT NULL,
  image_uri    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
  ts            TIMESTAMPTZ NOT NULL,
  reading_id    TEXT NOT NULL,
  instrument_id TEXT REFERENCES instruments(instrument_id),
  frame_id      TEXT REFERENCES frames(frame_id),
  value         DOUBLE PRECISION,
  unit          TEXT,
  raw_text      TEXT,
  display_page  TEXT,
  method        TEXT NOT NULL CHECK (method IN ('geometry','llm','ocr','manual')),
  confidence    DOUBLE PRECISION,             -- 0..1
  status        TEXT NOT NULL CHECK (status IN ('pending','accepted','rejected','needs_review','corrected')),
  attempt       SMALLINT NOT NULL DEFAULT 1,  -- 1 = first read, 2+ = re-look
  supersedes    TEXT,                         -- reading_id this one corrected
  reused_from   TEXT,                         -- reading_id reused because the crop did not change
  crop_uri      TEXT,
  model_call_id TEXT,
  PRIMARY KEY (ts, reading_id),
  UNIQUE (ts, frame_id, instrument_id, attempt)          -- retried jobs never duplicate readings
);
CREATE INDEX IF NOT EXISTS readings_instrument_ts ON readings (instrument_id, ts DESC);

CREATE TABLE IF NOT EXISTS physics_checks (
  check_id           TEXT PRIMARY KEY,
  ts                 TIMESTAMPTZ NOT NULL,
  equipment_id       TEXT REFERENCES equipment(equipment_id),
  rule_id            TEXT NOT NULL,
  reading_ids        TEXT[] NOT NULL,
  expected           DOUBLE PRECISION,
  observed           DOUBLE PRECISION,
  residual_pct       DOUBLE PRECISION,
  tolerance_used     DOUBLE PRECISION,
  passed             BOOLEAN NOT NULL,
  steady_state       BOOLEAN,
  input_ages_s       JSONB,
  suspect_reading_id TEXT,
  candidate_fix      JSONB,
  outcome            TEXT CHECK (outcome IN ('pass','misread_fixed','persistent_violation','needs_review','skipped'))
);
CREATE INDEX IF NOT EXISTS physics_checks_equipment_ts ON physics_checks (equipment_id, ts DESC);
ALTER TABLE physics_checks ADD COLUMN IF NOT EXISTS clip_id TEXT;
ALTER TABLE physics_checks ADD COLUMN IF NOT EXISTS frame_id TEXT;
ALTER TABLE physics_checks ADD COLUMN IF NOT EXISTS reason TEXT;      -- why it passed, failed or was skipped
ALTER TABLE physics_checks ADD COLUMN IF NOT EXISTS relook JSONB;     -- re-look attempts: strategy, value, crop, outcome

CREATE TABLE IF NOT EXISTS anomalies (
  anomaly_id   TEXT PRIMARY KEY,
  ts_start     TIMESTAMPTZ NOT NULL,
  ts_end       TIMESTAMPTZ,
  equipment_id TEXT REFERENCES equipment(equipment_id),
  kind         TEXT CHECK (kind IN ('threshold','rate_of_change','zscore','physics')),
  severity     TEXT CHECK (severity IN ('info','warning','critical')),
  summary      TEXT,
  evidence     JSONB                          -- reading_ids, frame_ids, check_ids
);

ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS clip_id TEXT;
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS rule_id TEXT;
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS is_simulated BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS diagnoses (
  diagnosis_id  TEXT PRIMARY KEY,
  anomaly_id    TEXT REFERENCES anomalies(anomaly_id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  model         TEXT,
  verified      BOOLEAN NOT NULL DEFAULT false,
  output        JSONB NOT NULL,               -- validated DiagnosisOutput (or the template)
  decision      TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','approved','rejected')),
  decided_by    TEXT,
  decided_at    TIMESTAMPTZ,
  decision_note TEXT
);

CREATE TABLE IF NOT EXISTS model_calls (
  call_id        TEXT PRIMARY KEY,
  ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
  provider       TEXT,
  model          TEXT,
  purpose        TEXT,                        -- read_digital | detect | relook | diagnose | ...
  prompt_version TEXT,
  latency_ms     INTEGER,
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  cost_usd       NUMERIC,
  cache_hit      BOOLEAN NOT NULL DEFAULT false,
  ok             BOOLEAN NOT NULL,
  error          TEXT
);

-- Work queue (docs/12_jobs_and_events.md)
CREATE TABLE IF NOT EXISTS jobs (
  job_id        TEXT PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('process_clip','diagnose','run_eval','demo_replay')),
  payload       JSONB NOT NULL,
  status        TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','dead')),
  attempts      SMALLINT NOT NULL DEFAULT 0,
  max_attempts  SMALLINT NOT NULL DEFAULT 3,
  run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),
  locked_by     TEXT,
  locked_until  TIMESTAMPTZ,
  progress      NUMERIC NOT NULL DEFAULT 0,
  last_error    TEXT,
  dedupe_key    TEXT UNIQUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, run_after);

-- Progress stream for the UI (SSE reads it by event_id)
CREATE TABLE IF NOT EXISTS clip_events (
  event_id   BIGSERIAL PRIMARY KEY,
  clip_id    TEXT NOT NULL,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  type       TEXT NOT NULL,
  payload    JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS clip_events_clip ON clip_events (clip_id, event_id);

-- Eval scenarios seed their own clips; the normal UI lists leave them out.
ALTER TABLE clips DROP CONSTRAINT IF EXISTS clips_source_channel_check;
ALTER TABLE clips ADD CONSTRAINT clips_source_channel_check
  CHECK (source_channel IN ('web','seed','replay','eval','imported'));

-- Agent traces, shaped like OpenTelemetry spans (GenAI semantic conventions in `attributes`), so they can be
-- exported over OTLP (Langfuse, any OTEL backend) later without changing what is recorded.
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id         TEXT PRIMARY KEY,               -- the trace id
  kind           TEXT NOT NULL DEFAULT 'diagnosis',
  anomaly_id     TEXT,
  diagnosis_id   TEXT,
  eval_run_id    TEXT,                           -- set when the run is part of an evaluation
  scenario_id    TEXT,
  model          TEXT,
  prompt_version TEXT,
  status         TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','error')),
  verified       BOOLEAN,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at       TIMESTAMPTZ,
  duration_ms    INTEGER,
  llm_calls      INTEGER NOT NULL DEFAULT 0,
  tool_calls     INTEGER NOT NULL DEFAULT 0,
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  error          TEXT
);
CREATE INDEX IF NOT EXISTS agent_runs_anomaly ON agent_runs (anomaly_id, started_at DESC);
CREATE INDEX IF NOT EXISTS agent_runs_started ON agent_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS agent_spans (
  span_id      TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  parent_id    TEXT,
  seq          INTEGER NOT NULL,                 -- start order within the run
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL,                    -- agent | context | llm | tool | guardrail | fallback
  status       TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','error')),
  started_at   TIMESTAMPTZ NOT NULL,
  ended_at     TIMESTAMPTZ,
  duration_ms  INTEGER,
  attributes   JSONB NOT NULL DEFAULT '{}',
  input        JSONB,
  output       JSONB
);
CREATE INDEX IF NOT EXISTS agent_spans_run ON agent_spans (run_id, seq);

-- Agent evaluation: fixed scenarios with known answers, scored deterministically (no LLM judge).
CREATE TABLE IF NOT EXISTS eval_runs (
  eval_run_id    TEXT PRIMARY KEY,
  status         TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('queued','running','done','failed')),
  model          TEXT,
  prompt_version TEXT,
  scenarios      INTEGER NOT NULL DEFAULT 0,
  passed         INTEGER NOT NULL DEFAULT 0,
  score          DOUBLE PRECISION,               -- mean of all criteria over all scenarios, 0..1
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at       TIMESTAMPTZ,
  error          TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
  eval_run_id  TEXT NOT NULL REFERENCES eval_runs(eval_run_id) ON DELETE CASCADE,
  scenario_id  TEXT NOT NULL,
  title        TEXT NOT NULL,
  run_id       TEXT,                             -- the agent trace
  anomaly_id   TEXT,
  diagnosis_id TEXT,
  passed       BOOLEAN NOT NULL,
  score        DOUBLE PRECISION NOT NULL,
  criteria     JSONB NOT NULL,                   -- {name: {"pass": bool, "detail": str}}
  headline     TEXT,
  duration_ms  INTEGER,
  error        TEXT,
  PRIMARY KEY (eval_run_id, scenario_id)
);

-- Questions about a clip, asked next to the footage. One conversation
-- per clip; a message may carry the frame the user was looking at and the anomaly page it came from.
CREATE TABLE IF NOT EXISTS chat_messages (
  message_id  TEXT PRIMARY KEY,
  clip_id     TEXT NOT NULL REFERENCES clips(clip_id),
  role        TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content     TEXT NOT NULL,
  frame_id    TEXT,                           -- user: the frame in view when asking
  anomaly_id  TEXT,                           -- user: asked from this anomaly's page
  citations   TEXT[] NOT NULL DEFAULT '{}',   -- assistant: evidence IDs in the answer
  verified    BOOLEAN,                        -- assistant: every cited ID and number checked out
  problems    TEXT[] NOT NULL DEFAULT '{}',
  run_id      TEXT,                           -- assistant: the agent trace
  cleared     BOOLEAN NOT NULL DEFAULT false, -- "new conversation" hides earlier turns (kept for the record)
  created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS chat_messages_clip ON chat_messages (clip_id, created_at);

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS clip_id TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS title TEXT;     -- e.g. the chat question

-- Automatic onboarding: equipment identified from a clip (nameplate + displays) registers itself.
ALTER TABLE equipment DROP CONSTRAINT IF EXISTS equipment_kind_check;
ALTER TABLE equipment ADD CONSTRAINT equipment_kind_check
  CHECK (kind IN ('inverter','charge_controller','generator','meter','pressure_system','clamp_meter',
                  'multimeter','other'));
ALTER TABLE equipment ADD COLUMN IF NOT EXISTS profile JSONB;   -- brand, model, ratings, limits, evidence, source
ALTER TABLE clips ADD COLUMN IF NOT EXISTS identification JSONB; -- what the clip was identified as, and how

-- Who can see what. Guests get a session that lives only in their open page: when it ends (refresh, close, 30 min
-- idle) everything they uploaded is deleted. Accounts keep their clips across visits. Admins see everything.
-- Clips and equipment without an owner (seed, eval, older rows) are visible to admins only.
CREATE TABLE IF NOT EXISTS users (
  user_id       TEXT PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,            -- stored lower-case
  password_hash TEXT NOT NULL,                   -- scrypt$n$r$p$salt$hash
  role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id    TEXT PRIMARY KEY,
  token_hash    TEXT NOT NULL UNIQUE,            -- sha256 of the bearer token; the token itself is never stored
  kind          TEXT NOT NULL CHECK (kind IN ('guest','user')),
  user_id       TEXT REFERENCES users(user_id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ,                     -- accounts: sliding expiry; guests: idle timeout instead
  ended_at      TIMESTAMPTZ,
  end_reason    TEXT,                            -- left | idle | logout | expired | signed_in
  purged_at     TIMESTAMPTZ                      -- guest data deleted
);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions (kind, ended_at, last_seen_at);

ALTER TABLE clips ADD COLUMN IF NOT EXISTS owner_user_id TEXT;
ALTER TABLE clips ADD COLUMN IF NOT EXISTS owner_session_id TEXT;
ALTER TABLE equipment ADD COLUMN IF NOT EXISTS owner_user_id TEXT;
ALTER TABLE equipment ADD COLUMN IF NOT EXISTS owner_session_id TEXT;
CREATE INDEX IF NOT EXISTS clips_owner_session ON clips (owner_session_id);
CREATE INDEX IF NOT EXISTS clips_owner_user ON clips (owner_user_id);

-- Photo clips: one or more photos instead of a video. {"kind": "photos", "timed": bool, "photos": [{"uri", "t"}]}
-- (t = seconds from the first photo: real capture times when every photo had one, else 300 s apart).
ALTER TABLE clips ADD COLUMN IF NOT EXISTS media JSONB;
