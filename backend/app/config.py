"""GaugeLens settings, read from the environment (falling back to the project's .env)."""

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# .env keys whose value was already set differently in the shell (the shell wins). A terminal that kept an old
# environment silently runs the wrong model or database password, so startup warns about these.
SHADOWED_ENV: list[str] = []


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Fills variables that aren't already set (in containers, settings come from docker-compose)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in os.environ and os.environ[key] != value and key not in SHADOWED_ENV:
            SHADOWED_ENV.append(key)
        os.environ.setdefault(key, value)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _flag(name: str, default: bool = False) -> bool:
    return (_env(name, str(default)) or "").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- Meta Model API (Muse Spark reads and reasons; SAM 3.1 segments)
    meta_api_key: str | None = None
    meta_base_url: str = "https://api.meta.ai/v1"
    meta_model_read: str = "muse-spark-1.3"
    temperature: float | None = None     # None = don't send (the model's default)

    # --- structured output and images
    structured_mode: str = "json_schema"  # json_schema (response_format, schema-constrained) | tool (one tool call)
    tool_choice: str = "auto"            # auto (the only value Meta accepts) | required | forced
    image_detail: str | None = None      # optional image detail hint; empty = don't send
    reasoning_effort: str | None = None  # sent only when set

    # --- cache
    cache_dir: Path = PROJECT_ROOT / "cache" / "model_responses"
    cache_only: bool = False

    # --- storage
    database_url: str | None = None
    evidence_dir: Path = PROJECT_ROOT / "evidence"   # the files (local store) or their cache (blob store)
    evidence_store: str = "local"        # local | blob (Azure Blob Storage)
    azure_storage_connection_string: str | None = None
    azure_blob_container: str = "guagelens"

    # --- pipeline
    seg_provider: str = "manual"         # manual (registered ROIs) | meta (SAM 3.1 finds displays per clip)
    worker_in_process: bool = True       # run the job worker inside the API process (v1 default)
    sample_fps: float = 2.0              # frames sampled per second of video
    max_frames: int = 60                 # long videos are sampled more sparsely so a clip stays within this
    reread_interval_s: float = 10.0      # re-read an unchanged display at least this often
    change_threshold: float = 10.0       # largest per-block grey-level difference (0-255) that counts as "changed"
    sam_track: bool = True               # with SEG_PROVIDER=meta: track displays through the clip (SAM 3.1 video)
    max_upload_mb: int = 100             # a 2-minute 1080p phone clip fits; uploads are held in memory
    max_relooks_per_clip: int = 10       # re-look budget (each re-look = up to 2 attempts)

    # --- limits
    request_timeout_s: float = 90.0
    max_retries: int = 2                 # transport retries on 429/5xx and timeouts
    max_schema_retries: int = 1          # re-asks when the model's JSON fails validation

    # --- access (guests, accounts, admin)
    guest_idle_minutes: int = 30         # a guest session with no activity this long ends and its data is deleted
    guest_max_clips: int = 5             # clips one guest session may upload (model credits)
    user_session_days: int = 30          # an account stays signed in this long after its last visit
    # per hour: guests by address (a new session doesn't reset them), accounts by account; admins unlimited (limits.py)
    guest_sessions_per_hour: int = 60    # generous: every page refresh starts a session
    guest_uploads_per_hour: int = 10
    guest_questions_per_hour: int = 40
    user_uploads_per_hour: int = 30
    user_questions_per_hour: int = 200

    @property
    def read_model(self) -> str:
        """The model that reads displays and runs the agent."""
        return self.meta_model_read


def get_settings() -> Settings:
    load_dotenv()
    temperature = _env("LLM_TEMPERATURE")
    return Settings(
        meta_api_key=_env("MODEL_API_KEY"),
        meta_base_url=_env("META_BASE_URL", "https://api.meta.ai/v1"),
        meta_model_read=_env("META_MODEL_READ", "muse-spark-1.3"),
        temperature=float(temperature) if temperature is not None else None,
        structured_mode=(_env("LLM_STRUCTURED_MODE", "json_schema") or "json_schema").lower(),
        tool_choice=(_env("LLM_TOOL_CHOICE", "auto") or "auto").lower(),
        image_detail=_env("LLM_IMAGE_DETAIL"),
        reasoning_effort=_env("LLM_REASONING_EFFORT"),
        cache_only=_flag("CACHE_ONLY"),
        database_url=_env("DATABASE_URL"),
        evidence_dir=Path(_env("EVIDENCE_DIR", str(PROJECT_ROOT / "evidence"))),
        evidence_store=(_env("EVIDENCE_STORE", "local") or "local").lower(),
        azure_storage_connection_string=_env("AZURE_STORAGE_CONNECTION_STRING"),
        azure_blob_container=_env("AZURE_BLOB_CONTAINER", "guagelens") or "guagelens",
        seg_provider=(_env("SEG_PROVIDER", "manual") or "manual").lower(),
        worker_in_process=_flag("WORKER_IN_PROCESS", True),
        sample_fps=float(_env("SAMPLE_FPS", "2.0")),
        max_frames=int(_env("MAX_FRAMES", "60")),
        reread_interval_s=float(_env("REREAD_INTERVAL_S", "10")),
        change_threshold=float(_env("CHANGE_THRESHOLD", "10.0")),
        sam_track=_flag("SAM_TRACK", True),
        guest_idle_minutes=int(_env("GUEST_IDLE_MINUTES", "30")),
        guest_max_clips=int(_env("GUEST_MAX_CLIPS", "5")),
        user_session_days=int(_env("USER_SESSION_DAYS", "30")),
        max_upload_mb=int(_env("MAX_UPLOAD_MB", "100")),
        guest_sessions_per_hour=int(_env("GUEST_SESSIONS_PER_HOUR", "60")),
        guest_uploads_per_hour=int(_env("GUEST_UPLOADS_PER_HOUR", "10")),
        guest_questions_per_hour=int(_env("GUEST_QUESTIONS_PER_HOUR", "40")),
        user_uploads_per_hour=int(_env("USER_UPLOADS_PER_HOUR", "30")),
        user_questions_per_hour=int(_env("USER_QUESTIONS_PER_HOUR", "200")),
    )


def missing_settings(settings: Settings) -> list[str]:
    """Required variables for the chosen mode that are not set (fail fast at startup)."""
    needed: dict[str, object] = {}
    if not settings.cache_only:
        needed = {"MODEL_API_KEY": settings.meta_api_key}
    if settings.evidence_store == "blob":
        needed["AZURE_STORAGE_CONNECTION_STRING"] = settings.azure_storage_connection_string
    return [name for name, value in needed.items() if not value]
