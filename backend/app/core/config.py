"""Application configuration via Pydantic Settings."""
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings loaded from environment / .env file.

    Missing required fields (e.g. JWT_SECRET_KEY in production) cause startup failure
    with a clear Pydantic ValidationError.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    APP_NAME: str = "Agent Flow"
    APP_ENV: str = "development"
    DEBUG: bool = False

    # MongoDB
    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "agent_flow"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    JWT_SECRET_KEY: str = "dev-only-not-for-production-replace-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # CORS - comma-separated origins
    # In development, use "*" to allow all origins; in production,
    # restrict to explicit origins (e.g. "https://app.example.com").
    CORS_ORIGINS: str = "*"

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_JSON_FORMAT: bool = False

    # LangSmith tracing (optional — set API key to enable).
    # When LANGSMITH_API_KEY is set, LangChain/LangGraph traces are sent to smith.langchain.com.
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "agent-flow"

    # Model API Key encryption (Base64-encoded 32-byte AES-256 key)
    # Generate: python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
    MODEL_ENCRYPTION_KEY: str = ""

    # Task concurrency limits
    TASK_GLOBAL_MAX_RUNNING: int = 50
    TASK_USER_MAX_RUNNING: int = 5

    # Session token budget (cumulative tokens per session before the agent is blocked).
    # Agents can override via their own max_tokens field (0 = use this default).
    DEFAULT_SESSION_MAX_TOKENS: int = 200_000

    # Task scheduler (poll interval in seconds; set to 0 to disable)
    TASK_SCHEDULER_POLL_INTERVAL: int = 10

    # Trigger scheduler (poll interval in seconds; set to 0 to disable).
    # The trigger scheduler polls the triggers collection for due cron/once
    # triggers and fires them. Accuracy = poll interval (10s is plenty for
    # scheduled workflows). This replaces the previous Celery eta self-chain
    # design, which suffered from Redis visibility_timeout re-delivery on
    # long-eta (monthly) jobs.
    TRIGGER_SCHEDULER_POLL_INTERVAL: int = 10

    # Skill filesystem — root directory where Skill files are materialized.
    # Each Skill lives under ``{SKILLS_CONTAINER_DIR}/{skill_name}/``.
    # None = derive from SKILLS_HOST_DIR (local dev).
    # Docker: set explicitly by docker-compose (e.g. /data/skills).
    SKILLS_CONTAINER_DIR: str | None = None
    # Host-side path for Skills (the one users configure in .env).
    SKILLS_HOST_DIR: str = "~/.agent-flow/skills"

    # Workspace filesystem — root directory for per-Session workspaces.
    # Layout: ``{WORKSPACES_CONTAINER_DIR}/{user_id}/{session_id}/{input,output,tmp}``.
    # None = derive from WORKSPACES_HOST_DIR (local dev).
    # Docker: set explicitly by docker-compose (e.g. /data/workspaces).
    WORKSPACES_CONTAINER_DIR: str | None = None
    # Host-side path for Workspaces (the one users configure in .env).
    WORKSPACES_HOST_DIR: str = "~/.agent-flow/workspaces"

    # Workspace retention — days to keep workspace files after Session deletion.
    WORKSPACE_RETENTION_DAYS: int = 30

    # Workspace quota — max bytes per workspace (default 500 MB).
    WORKSPACE_MAX_BYTES: int = 500 * 1024 * 1024

    # Knowledge Base filesystem — root directory where KB .md files live.
    # Each KB lives under ``{KB_CONTAINER_DIR}/{kb_id}/`` (tree-style: agents
    # explore it at runtime via kb_glob/kb_grep/kb_read, no index, no vector).
    # None = derive from KB_HOST_DIR (local dev).
    # Docker: set explicitly by docker-compose (e.g. /data/knowledge_bases).
    KB_CONTAINER_DIR: str | None = None
    # Host-side path for Knowledge Bases (the one users configure in .env).
    KB_HOST_DIR: str = "~/.agent-flow/knowledge_bases"

    # Knowledge Base tool limits (truncation / caps, aligned with langxin tree KB).
    KB_GLOB_MAX_RESULTS: int = 200
    KB_GREP_MAX_FILES: int = 50
    KB_GREP_MAX_MATCHES: int = 200
    KB_READ_MAX_BYTES: int = 16_384
    KB_MAX_FILE_SIZE: int = 2 * 1024 * 1024  # 2 MB per uploaded .md

    @model_validator(mode="after")
    def _default_internal_dirs_from_host(self) -> "Settings":
        """Default container-internal dirs to host dirs when not explicitly set.

        - Local dev: user sets only ``*_HOST_DIR``; ``*_CONTAINER_DIR`` is None → derive from host.
        - Docker: docker-compose injects ``WORKSPACES_CONTAINER_DIR``/``SKILLS_CONTAINER_DIR`` explicitly.
        """
        import os
        # Expand ~ in host dirs so Docker can use them directly
        self.WORKSPACES_HOST_DIR = os.path.expanduser(self.WORKSPACES_HOST_DIR)
        self.SKILLS_HOST_DIR = os.path.expanduser(self.SKILLS_HOST_DIR)
        self.KB_HOST_DIR = os.path.expanduser(self.KB_HOST_DIR)
        if self.WORKSPACES_CONTAINER_DIR is None:
            self.WORKSPACES_CONTAINER_DIR = self.WORKSPACES_HOST_DIR
        if self.SKILLS_CONTAINER_DIR is None:
            self.SKILLS_CONTAINER_DIR = self.SKILLS_HOST_DIR
        if self.KB_CONTAINER_DIR is None:
            self.KB_CONTAINER_DIR = self.KB_HOST_DIR
        return self

    # ── Sandbox ──────────────────────────────────────────────────────────
    # Docker image used for bash tool sandbox execution.
    SANDBOX_IMAGE: str = "agent-sandbox:latest"

    # Sandbox resource limits.
    SANDBOX_MEM_LIMIT: str = "512m"
    SANDBOX_CPU_QUOTA: int = 100_000  # 1 CPU core (100000 μs quota per 100000 μs period)
    SANDBOX_TIMEOUT: int = 120  # seconds
    SANDBOX_MAX_OUTPUT_BYTES: int = 50 * 1024  # 50 KB stdout/stderr cap

    # When True, bash runs inside Docker container.
    # When False (default for local dev), bash runs via subprocess.
    SANDBOX_ENABLED: bool = False

    # When True, legacy sessions (created before checkpointer) have their
    # MessageRecord history serialized into the thread on first access.
    # Once migrated the thread is non-empty and subsequent requests use the
    # thread as the single source of LLM context. Disable after all legacy
    # sessions have been migrated.
    MIGRATE_LEGACY_SESSIONS: bool = False

    # Network mode for sandbox containers.
    # "none" = no network access (most secure, default)
    # "bridge" = standard Docker bridge network (allows outbound internet)
    # "host" = use host network stack (least isolation)
    SANDBOX_NETWORK_MODE: str = "none"

    # Container-internal mount points for sandbox containers.
    # These are the paths *inside* the sandbox container where workspace
    # and skill directories are mounted.
    SANDBOX_CONTAINER_WORKSPACE_DIR: str = "/workspace"
    SANDBOX_CONTAINER_SKILLS_DIR: str = "/data/skills"

    # ── Channels (inbound IM integrations) ──
    CHANNEL_INBOUND_ACK_TIMEOUT_MS: int = 2000
    CHANNEL_EVENT_LOG_TTL_HOURS: int = 24
    CHANNEL_MAX_RETRIES: int = 3
    CHANNEL_SEND_MAX_RETRIES: int = 3
    CHANNEL_DEFAULT_REPLY_ON_FAILURE: str = "处理失败,请稍后重试或联系管理员"
    CHANNEL_DEGRADED_ON_CONSECUTIVE_FAILURES: int = 5

    # ── Channels / long-connection (no-public-URL receive mode) ──
    # Per-provider master switches. Set to False to disable long-connection
    # entirely for a provider (channels fall back to webhook mode).
    CHANNEL_LARK_LONG_CONNECTION_ENABLED: bool = True
    CHANNEL_DINGTALK_LONG_CONNECTION_ENABLED: bool = True
    CHANNEL_WECOM_LONG_CONNECTION_ENABLED: bool = False  # no SDK yet
    CHANNEL_CONNECTION_RECONNECT_INTERVAL: int = 10  # seconds between retries
    CHANNEL_CONNECTION_STARTUP_DELAY: float = 2.0  # startup grace before first connect
    # Long-connection mode executes inbound messages directly in the FastAPI
    # process (no Celery). These tune that in-process execution:
    #   - EXECUTION_MAX_RETRIES: retry count for TransientChannelError
    #     (LLM rate limit / tool blip) with exponential backoff.
    #   - MAX_CONCURRENT_EXECUTIONS_PER_CHANNEL: per-channel semaphore cap to
    #     prevent a single busy chat from exhausting the LLM quota. Excess
    #     messages queue inside dispatch_inbound until a slot frees up.
    CHANNEL_EXECUTION_MAX_RETRIES: int = 3
    CHANNEL_MAX_CONCURRENT_EXECUTIONS_PER_CHANNEL: int = 4



settings = Settings()
