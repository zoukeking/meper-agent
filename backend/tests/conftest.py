"""Shared pytest fixtures for the backend test suite."""
import os

# Ensure tests don't require a real DB / Redis during import-time config load
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-not-for-prod")
# Fixed 32-byte key (Base64) for channel/crypto tests that round-trip
# encrypt_secret → decrypt_secret. NOT for production — test fixture only.
os.environ.setdefault(
    "MODEL_ENCRYPTION_KEY",
    "ZDswO0/08pEWbyhmnBhMZ6L0Sf/esm7VlhjI0Mx8h6A=",
)

import pytest
from app.main import app
from app.workers.celery_app import celery_app
from fastapi.testclient import TestClient

# ── Celery eager mode for tests ──
# Run Celery tasks synchronously in-process during tests instead of
# dispatching them to a real Redis broker. This prevents test-triggered
# .delay() calls from leaking into a running worker (which would execute
# them against test data and pollute logs with "task not found" errors).
celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True


@pytest.fixture
def client() -> TestClient:
    """A FastAPI TestClient that bypasses real network IO."""
    return TestClient(app)
