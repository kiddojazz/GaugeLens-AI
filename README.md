# GaugeLens

> GaugeLens turns the displays and gauges on off-grid power equipment into trustworthy telemetry. It catches its own misreadings using electrical physics, and it diagnoses real faults with visual evidence.

Meta Global AI Developer Hackathon 2026 · built on the Meta Model API: Muse Spark and SAM 3.1.

## Quick start (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements.txt
copy .env.example .env          # then fill in MODEL_API_KEY and POSTGRES_PASSWORD

.venv\Scripts\python -m backend.app.cli check     # which settings are missing
.venv\Scripts\python -m backend.app.cli ping      # Spike A: one tiny model call
.venv\Scripts\python -m pytest                    # offline tests (fake model, no network)
```

### Read a display (Spike B)

```powershell
.venv\Scripts\python -m backend.app.cli read path\to\photo.jpg --context "inverter, 24 V, 3.5 kVA"
.venv\Scripts\python -m backend.app.cli read path\to\folder\        # every image in a folder
.venv\Scripts\python -m backend.app.cli read photo.jpg --box 410,220,380,160   # crop x,y,w,h first
```

Results are printed and saved to `eval/spikes/results/read_<time>.jsonl`. Responses are cached in `cache/`, so re-running the same images costs nothing.

`--context` describes the equipment. **Never put expected values in it.**

### Mock panel (test without real footage)

Open `eval/synth/mock_panel/index.html` in a browser, full-screen it (F11) and film it with your phone.

**Keys:**

| Key | Action |
|---|---|
| R | Start/stop the ground-truth log |
| L | Kettle on/off (load step) |
| F | Hidden DC fault |
| G | Glare |
| N / P | Next page / hold page |
| 1 / 2 / 3 | Speed 1× / 30× / 120× |
| D | Download the ground-truth CSV |
| H | Hide the controls |

The ground-truth CSV uses the `readings.csv` format. The visible `REC T+` timer lets you match video frames to log rows. Mock panel results are **synthetic**: use them for development only.

### Database

The database runs on host port **5433**, because a native Windows PostgreSQL often holds 5432. In `DATABASE_URL`, use `127.0.0.1` rather than `localhost`: on Windows, `localhost` tries IPv6 first and adds about 2 s to every connection.

```powershell
docker compose up -d db
.venv\Scripts\python -m backend.app.db.init_db          # schema (safe to re-run)
.venv\Scripts\python -m backend.app.seed mock           # mock panel equipment, displays, instruments
```

### Process a clip (Day 1 pipeline)

```powershell
# Build a test video from the mock panel, with exact ground truth (needs Edge)
.venv\Scripts\python -m eval.synth.make_mock_clip --name mock_day1 --states 9 --hold 1.5 --ghost faint

# Upload -> job queue -> worker -> frames -> reads (unchanged crops reused) -> readings, scored vs truth
.venv\Scripts\python -m backend.app.cli process data\mock_clips\mock_day1.mp4 --equipment inv_mock --truth data\mock_clips\mock_day1_truth.csv
```

### API (the worker runs inside it)

```powershell
.venv\Scripts\uvicorn backend.app.main:app --reload --port 8000
```

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Database and model status |
| POST | `/api/v1/clips` | Upload a clip (multipart: `file`, `equipment_id`) |
| GET | `/api/v1/clips/{id}` | Clip status and job progress |
| GET | `/api/v1/clips/{id}/events` | Live progress (SSE) |
| GET | `/api/v1/readings?clip_id=` | Stored readings |
| GET | `/api/v1/equipment` | Equipment, displays and instruments |
| POST | `/api/v1/jobs/{id}/retry` | Retry a dead job |

To run the API in Docker instead, use `docker compose up -d api` (port 8000).

Tests use a separate `gaugelens_test` database, created automatically and wiped between tests. Run them with `.venv\Scripts\python -m pytest` (34 tests, about 4 s).

## Model

Everything runs on the **Meta Model API**: **Muse Spark** (`muse-spark-1.3`) reads the displays, runs the
diagnosis agent and answers questions; **SAM 3.1** finds and tracks the displays in each clip. The client is
our own small HTTP client ([backend/app/llm/meta_api.py](backend/app/llm/meta_api.py)); structured output
uses a JSON schema, validated with Pydantic and retried once with the validation error.
