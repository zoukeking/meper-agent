"""Tests for Story 8.6 — Rate Limiting & Monitoring."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.api.v1.ext import auth_and_rate_limit
from app.core.auth_apikey import ApiKeyPrincipal
from app.core.rate_limiter import check_rate_limit
from app.main import app
from app.services.api_key_stats_service import get_stats, record_request
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def full_principal():
    return ApiKeyPrincipal(
        key_id="apikey_test",
        owner_user_id="user_owner",
        scopes=["agents:read", "agents:invoke", "workflows:read", "workflows:invoke", "executions:read"],
        bindings={"agents": [], "workflows": []},
        rate_limit=60,
    )


def _override_auth(principal):
    """Override auth+rate_limit dependency, also mocking rate limiter."""
    from starlette.requests import Request

    async def _fake_dep(request: Request):
        request.state.api_key_id = principal.key_id
        request.state.rate_limit = principal.rate_limit
        request.state.rate_remaining = principal.rate_limit - 1
        request.state.rate_reset = 9999999999
        return principal
    app.dependency_overrides[auth_and_rate_limit] = _fake_dep
    return lambda: app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Rate limiter unit tests
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Unit tests for the sliding window rate limiter."""

    @pytest.mark.asyncio
    async def test_allowed_within_limit(self):
        """Request within limit should be allowed."""
        pipe = MagicMock()
        pipe.execute = AsyncMock(return_value=[0, 5, 1, 1])
        mock_redis = MagicMock()
        mock_redis.pipeline.return_value = pipe

        with patch("app.core.rate_limiter.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            allowed, remaining, reset_ts = await check_rate_limit("key1", limit=10)

        assert allowed is True
        assert remaining == 4  # 10 - 5 - 1

    @pytest.mark.asyncio
    async def test_denied_over_limit(self):
        """Request exceeding limit should be denied."""
        pipe = MagicMock()
        pipe.execute = AsyncMock(return_value=[0, 10, 1, 1])
        mock_redis = MagicMock()
        mock_redis.pipeline.return_value = pipe
        mock_redis.zrem = AsyncMock()

        with patch("app.core.rate_limiter.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            allowed, remaining, reset_ts = await check_rate_limit("key1", limit=10)

        assert allowed is False
        assert remaining == 0


# ---------------------------------------------------------------------------
# API Key stats unit tests
# ---------------------------------------------------------------------------


class TestApiKeyStats:
    """Unit tests for the stats recording and retrieval."""

    @pytest.mark.asyncio
    async def test_record_request(self):
        """record_request should call pipeline correctly."""
        pipe = MagicMock()
        pipe.execute = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.pipeline.return_value = pipe

        with patch("app.services.api_key_stats_service.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            await record_request("key1", "agents:read", 200)

        pipe.hincrby.assert_any_call("api_key_stats:key1", "total_requests", 1)
        pipe.hincrby.assert_any_call("api_key_stats:key1", "endpoint:agents:read", 1)
        pipe.hincrby.assert_any_call("api_key_stats:key1", "successful", 1)
        pipe.hset.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_failed_request(self):
        """Non-2xx status should increment failed counter."""
        pipe = MagicMock()
        pipe.execute = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.pipeline.return_value = pipe

        with patch("app.services.api_key_stats_service.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            await record_request("key1", "agents:invoke", 500)

        pipe.hincrby.assert_any_call("api_key_stats:key1", "failed", 1)

    @pytest.mark.asyncio
    async def test_get_stats_empty(self):
        """get_stats with no data returns zeros."""
        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={})

        with patch("app.services.api_key_stats_service.get_redis_client", return_value=mock_redis):
            stats = await get_stats("key_nonexistent")

        assert stats["total_requests"] == 0
        assert stats["by_endpoint"] == {}

    @pytest.mark.asyncio
    async def test_get_stats_with_data(self):
        """get_stats should parse Redis hash correctly."""
        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={
            "total_requests": "100",
            "successful": "95",
            "failed": "5",
            "endpoint:agents:read": "50",
            "endpoint:agents:invoke": "50",
            "last_used_at": "1700000000",
        })

        with patch("app.services.api_key_stats_service.get_redis_client", return_value=mock_redis):
            stats = await get_stats("key1")

        assert stats["total_requests"] == 100
        assert stats["successful"] == 95
        assert stats["failed"] == 5
        assert stats["by_endpoint"] == {"agents:read": 50, "agents:invoke": 50}
        assert stats["last_used_at"] is not None


# ---------------------------------------------------------------------------
# Rate limit headers (integration)
# ---------------------------------------------------------------------------


class TestRateLimitHeaders:
    """Integration tests for X-RateLimit-* response headers."""

    def test_rate_limit_headers_present(self, client, full_principal):
        """Ext API responses should include X-RateLimit-* headers."""
        app.dependency_overrides.clear()  # ensure clean state
        cleanup = _override_auth(full_principal)
        try:
            with (
                patch("app.api.v1.ext.record_request", new_callable=AsyncMock),
                patch("app.services.agent_service.AgentService.list_agents", new_callable=AsyncMock) as mock_list,
            ):
                mock_list.return_value = ([], 0)
                resp = client.get("/api/v1/ext/agents")
            assert resp.status_code == 200
            assert "x-ratelimit-limit" in resp.headers
            assert "x-ratelimit-remaining" in resp.headers
            assert "x-ratelimit-reset" in resp.headers
        finally:
            cleanup()

    def test_no_rate_limit_headers_for_non_ext(self, client):
        """Non-ext routes should NOT have X-RateLimit-* headers."""
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert "x-ratelimit-limit" not in resp.headers


# ---------------------------------------------------------------------------
# Stats endpoint (integration)
# ---------------------------------------------------------------------------


class TestStatsEndpoint:
    """Integration tests for GET /api/v1/api-keys/{id}/stats."""

    def test_get_stats_endpoint(self, client):
        """Stats endpoint should return aggregated data."""
        # Mock JWT auth
        from app.core.security import get_current_user, require_role
        from app.schemas.user import UserResponse

        app.dependency_overrides.clear()  # ensure clean state
        mock_user = UserResponse(
            id="user_admin",
            username="admin",
            email="admin@test.com",
            name="Admin",
            role="admin",
            status="active",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
        app.dependency_overrides[get_current_user] = lambda: mock_user
        app.dependency_overrides[require_role] = lambda: mock_user

        try:
            with (
                patch("app.api.v1.api_keys.ApiKeyService.get_api_key", new_callable=AsyncMock) as mock_get,
                patch("app.api.v1.api_keys.get_stats", new_callable=AsyncMock) as mock_stats,
                patch(
                    "app.services.execution_log_service.ExecutionLogService.get_token_summary_by_api_key",
                    new_callable=AsyncMock,
                ) as mock_tokens,
                patch(
                    "app.services.execution_log_service.ExecutionLogService.get_users_summary_by_api_key",
                    new_callable=AsyncMock,
                ) as mock_users,
            ):
                mock_get.return_value = {"_id": "apikey_01", "name": "Test Key"}
                mock_stats.return_value = {
                    "api_key_id": "apikey_01",
                    "total_requests": 42,
                    "successful": 40,
                    "failed": 2,
                    "by_endpoint": {"agents:read": 42},
                    "last_used_at": "2026-01-01T00:00:00+00:00",
                }
                mock_tokens.return_value = {
                    "total_tokens": 500,
                    "input_tokens": 300,
                    "output_tokens": 200,
                    "calls": 42,
                }
                mock_users.return_value = [
                    {"user_sub": "user-A", "calls": 10, "total_tokens": 100, "last_seen_at": "2026-07-21T00:00:00"},
                ]
                resp = client.get("/api/v1/api-keys/apikey_01/stats")

            assert resp.status_code == 200
            data = resp.json()
            assert data["total_requests"] == 42
            assert data["successful"] == 40
            # P3: token fields are now part of the response.
            assert data["total_tokens"] == 500
            assert data["unique_users"] == 1
        finally:
            app.dependency_overrides.clear()

    def test_get_stats_not_found(self, client):
        """Stats endpoint returns 404 for nonexistent API Key."""
        from app.core.security import get_current_user, require_role
        from app.schemas.user import UserResponse

        mock_user = UserResponse(
            id="user_admin",
            username="admin",
            email="admin@test.com",
            name="Admin",
            role="admin",
            status="active",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
        app.dependency_overrides[get_current_user] = lambda: mock_user
        app.dependency_overrides[require_role] = lambda: mock_user

        try:
            with patch("app.api.v1.api_keys.ApiKeyService.get_api_key", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = None
                resp = client.get("/api/v1/api-keys/apikey_nonexistent/stats")

            assert resp.status_code == 404
        finally:
            app.dependency_overrides.clear()
