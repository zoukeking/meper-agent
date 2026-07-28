# syntax=docker/dockerfile:1.7

# --- Builder stage: use uv to install prod deps in a slim venv ---
FROM python:3.12-slim AS builder

# Install uv via pip (avoids ghcr.io network issues)
RUN pip install --no-cache-dir uv

WORKDIR /app

# Leverage layer caching: copy lockfiles first
COPY pyproject.toml uv.lock .python-version ./

# The local harness package is an editable path dep (packages/harness), so its
# source must be present before `uv sync --frozen` can resolve the install plan.
COPY packages/harness packages/harness

# Install production dependencies into the project virtualenv (no dev, no project itself yet)
RUN uv sync --frozen --no-dev --no-install-project

# --- Runtime stage: minimal image with non-root user ---
FROM python:3.12-slim AS runtime

WORKDIR /app

# Create non-root user.
# UID/GID 1001 must match the sandbox user (deploy/Dockerfile.sandbox) so that
# files created in the shared workspace volume are accessible from both sides.
ARG APP_UID=1001
ARG APP_GID=1001
RUN groupadd -r -g ${APP_GID} appuser \
    && useradd -r -g appuser -u ${APP_UID} -m -s /bin/bash appuser \
    # gosu lets the entrypoint chown bind mounts as root, then drop to appuser.
    && apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy the venv from builder
COPY --from=builder /app/.venv /app/.venv

# The venv installs harness as editable (pointing at /app/packages/harness), so
# the harness source must be present at runtime too.
COPY packages/harness packages/harness

# Copy application code
COPY app/ app/
COPY scripts/ scripts/
COPY pyproject.toml uv.lock .python-version ./

# Create logs directory for non-root user
RUN mkdir -p /app/logs && chown -R appuser:appuser /app

# Entrypoint: fix ownership of bind-mounted volumes as root, then drop to appuser.
# Inlined here (via BuildKit heredoc) so no separate script file is needed.
COPY <<-'EOF' /usr/local/bin/docker-entrypoint.sh
#!/bin/bash
# Backend container entrypoint.
#
# Bind-mounted volumes (/data/workspaces, /data/skills) are owned by whoever
# created them on the host — often root when the host is deployed as root.
# This entrypoint runs as root, fixes ownership so the non-root appuser
# (UID 1001, matching the sandbox container) can read/write, then drops
# privileges via gosu before exec'ing the real command (uvicorn / celery).
set -e

APP_USER="appuser"

# Directories that are bind-mounted and must be writable by appuser.
# Derived from the *_CONTAINER_DIR settings with sane defaults.
WORKSPACES_DIR="${WORKSPACES_CONTAINER_DIR:-/data/workspaces}"
SKILLS_DIR="${SKILLS_CONTAINER_DIR:-/data/skills}"

for dir in "$WORKSPACES_DIR" "$SKILLS_DIR" /app/logs; do
    mkdir -p "$dir"
    chown -R "$APP_USER":"$APP_USER" "$dir"
done

exec gosu "$APP_USER" "$@"
EOF
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Ensure the venv takes precedence on PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Start as root; the entrypoint chowns volumes then execs the command as appuser.
ENTRYPOINT ["docker-entrypoint.sh"]

EXPOSE 8000

# Healthcheck against the unauthenticated /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx, sys; sys.exit(0 if httpx.get('http://localhost:8000/health', timeout=3).status_code == 200 else 1)"

# Start Uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
