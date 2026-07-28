"""Tests for internal API Key management endpoints (admin-only, JWT auth)."""
from unittest.mock import AsyncMock, patch

import pytest
from app.core.security import get_current_user, require_role
from app.main import app
from app.models.user import UserStatus
from app.schemas.user import UserResponse
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin_user():
    return UserResponse(
        id="user_admin",
        username="admin",
        email="admin@test.com",
        role="admin",
        status=UserStatus.ACTIVE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


def _override_auth(admin_user):
    """Override JWT auth + role check dependencies."""
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_role] = lambda: admin_user
    return lambda: app.dependency_overrides.clear()


def _make_key_doc(key_id="apikey_01", name="Test Key", status="active"):
    return {
        "_id": key_id,
        "name": name,
        "key_hash": "hashed",
        "key_prefix": "af_live_abcd",
        "owner_user_id": "user_admin",
        "scopes": ["agents:read", "agents:invoke"],
        "bindings": {"agents": [], "workflows": []},
        "rate_limit": 60,
        "status": status,
        "expires_at": None,
        "last_used_at": None,
        "user_info_url": "",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }


# ---------------------------------------------------------------------------
# AC-1: Create API Key
# ---------------------------------------------------------------------------


class TestCreateApiKey:
    """POST /api/v1/api-keys"""

    def test_create_success(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            doc = _make_key_doc()
            raw_key = "af_live_abcdefghijklmnopqrstuvwxyz12"
            with patch(
                "app.services.api_key_service.ApiKeyService.create_api_key",
                new=AsyncMock(return_value=(doc, raw_key)),
            ):
                resp = client.post(
                    "/api/v1/api-keys",
                    json={
                        "name": "Test Key",
                        "scopes": ["agents:read", "agents:invoke"],
                    },
                )
            assert resp.status_code == 201
            data = resp.json()
            assert data["id"] == "apikey_01"
            assert data["key"] == raw_key  # raw key returned once
            assert data["key_prefix"] == "af_live_abcd"
            assert data["name"] == "Test Key"
            assert data["status"] == "active"
        finally:
            cleanup()

    def test_create_with_bindings(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            doc = _make_key_doc()
            doc["bindings"] = {"agents": ["agent_01"], "workflows": []}
            with patch(
                "app.services.api_key_service.ApiKeyService.create_api_key",
                new=AsyncMock(return_value=(doc, "af_live_test")),
            ) as mock_create:
                resp = client.post(
                    "/api/v1/api-keys",
                    json={
                        "name": "Bound Key",
                        "scopes": ["agents:read"],
                        "bindings": {
                            "agents": ["agent_01"],
                            "workflows": [],
                        },
                    },
                )
            assert resp.status_code == 201
            # Verify bindings were passed to service
            call_kwargs = mock_create.call_args.kwargs
            assert call_kwargs["bindings"] == {"agents": ["agent_01"], "workflows": []}
        finally:
            cleanup()

    def test_create_with_user_info_url(self, client, admin_user) -> None:
        """AC2: user_info_url is forwarded to the service on create."""
        cleanup = _override_auth(admin_user)
        try:
            doc = _make_key_doc()
            doc["user_info_url"] = "https://partner.example.com/introspect"
            with patch(
                "app.services.api_key_service.ApiKeyService.create_api_key",
                new=AsyncMock(return_value=(doc, "af_live_test")),
            ) as mock_create:
                resp = client.post(
                    "/api/v1/api-keys",
                    json={
                        "name": "Callback Key",
                        "scopes": ["agents:read"],
                        "user_info_url": "https://partner.example.com/introspect",
                    },
                )
            assert resp.status_code == 201
            data = resp.json()
            assert (
                data["user_info_url"]
                == "https://partner.example.com/introspect"
            )
            call_kwargs = mock_create.call_args.kwargs
            assert (
                call_kwargs["user_info_url"]
                == "https://partner.example.com/introspect"
            )
        finally:
            cleanup()

    def test_create_without_user_info_url_defaults_empty(
        self, client, admin_user
    ) -> None:
        """AC2: omitted user_info_url forwards empty string (legacy mode)."""
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.api_key_service.ApiKeyService.create_api_key",
                new=AsyncMock(return_value=(_make_key_doc(), "af_live_test")),
            ) as mock_create:
                client.post(
                    "/api/v1/api-keys",
                    json={"name": "Legacy", "scopes": ["agents:read"]},
                )
            call_kwargs = mock_create.call_args.kwargs
            assert call_kwargs["user_info_url"] == ""
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# AC-2 & AC-3: List API Keys (masked)
# ---------------------------------------------------------------------------


class TestListApiKeys:
    """GET /api/v1/api-keys"""

    def test_list_success(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            keys = [_make_key_doc(), _make_key_doc("apikey_02", "Key 2")]
            with patch(
                "app.services.api_key_service.ApiKeyService.list_api_keys",
                new=AsyncMock(return_value=(keys, 2)),
            ):
                resp = client.get("/api/v1/api-keys")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 2
            assert len(data["items"]) == 2
            # Verify no key_hash or raw key in response
            item = data["items"][0]
            assert "key_hash" not in item
            assert "key" not in item
            assert item["key_prefix"] == "af_live_abcd"
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# AC-3: Get API Key detail
# ---------------------------------------------------------------------------


class TestGetApiKey:
    """GET /api/v1/api-keys/{id}"""

    def test_get_success(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.api_key_service.ApiKeyService.get_api_key",
                new=AsyncMock(return_value=_make_key_doc()),
            ):
                resp = client.get("/api/v1/api-keys/apikey_01")
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == "apikey_01"
            assert "key" not in data  # no raw key
            assert "key_hash" not in data
        finally:
            cleanup()

    def test_get_not_found(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.api_key_service.ApiKeyService.get_api_key",
                new=AsyncMock(return_value=None),
            ):
                resp = client.get("/api/v1/api-keys/nonexistent")
            assert resp.status_code == 404
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# AC-4: Update API Key
# ---------------------------------------------------------------------------


class TestUpdateApiKey:
    """PUT /api/v1/api-keys/{id}"""

    def test_update_success(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            doc = _make_key_doc()
            doc["scopes"] = ["agents:read", "agents:invoke", "executions:read"]
            with patch(
                "app.services.api_key_service.ApiKeyService.update_api_key",
                new=AsyncMock(return_value=doc),
            ):
                resp = client.put(
                    "/api/v1/api-keys/apikey_01",
                    json={"scopes": ["agents:read", "agents:invoke", "executions:read"]},
                )
            assert resp.status_code == 200
            data = resp.json()
            assert "executions:read" in data["scopes"]
        finally:
            cleanup()

    def test_update_not_found(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.api_key_service.ApiKeyService.update_api_key",
                new=AsyncMock(return_value=None),
            ):
                resp = client.put(
                    "/api/v1/api-keys/nonexistent",
                    json={"name": "New Name"},
                )
            assert resp.status_code == 404
        finally:
            cleanup()

    def test_update_user_info_url(self, client, admin_user) -> None:
        """AC2: user_info_url can be set/cleared via update."""
        cleanup = _override_auth(admin_user)
        try:
            doc = _make_key_doc()
            doc["user_info_url"] = "https://partner.example.com/introspect"
            with patch(
                "app.services.api_key_service.ApiKeyService.update_api_key",
                new=AsyncMock(return_value=doc),
            ) as mock_update:
                resp = client.put(
                    "/api/v1/api-keys/apikey_01",
                    json={
                        "user_info_url": "https://partner.example.com/introspect"
                    },
                )
            assert resp.status_code == 200
            data = resp.json()
            assert (
                data["user_info_url"]
                == "https://partner.example.com/introspect"
            )
            call_kwargs = mock_update.call_args.kwargs
            assert (
                call_kwargs["user_info_url"]
                == "https://partner.example.com/introspect"
            )
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# AC-5: Revoke API Key
# ---------------------------------------------------------------------------


class TestRevokeApiKey:
    """DELETE /api/v1/api-keys/{id}"""

    def test_revoke_success(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.api_key_service.ApiKeyService.revoke_api_key",
                new=AsyncMock(return_value=_make_key_doc(status="revoked")),
            ):
                resp = client.delete("/api/v1/api-keys/apikey_01")
            assert resp.status_code == 204
        finally:
            cleanup()

    def test_revoke_not_found(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.api_key_service.ApiKeyService.revoke_api_key",
                new=AsyncMock(return_value=None),
            ):
                resp = client.delete("/api/v1/api-keys/nonexistent")
            assert resp.status_code == 404
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# Logs & Users endpoints (Story 8.2 P3)
# ---------------------------------------------------------------------------


class TestApiKeyLogsEndpoint:
    """GET /api/v1/api-keys/{id}/logs"""

    def test_logs_returns_paginated_results(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            with (
                patch(
                    "app.api.v1.api_keys.ApiKeyService.get_api_key",
                    new=AsyncMock(return_value=_make_key_doc()),
                ),
                patch(
                    "app.services.execution_log_service.ExecutionLogService.list_logs_by_api_key",
                    new=AsyncMock(return_value=([
                        {
                            "api_key_id": "apikey_01",
                            "owner_user_id": "user_admin",
                            "user_sub": "user-A",
                            "endpoint": "agents:invoke",
                            "total_tokens": 100,
                            "timestamp": "2026-07-21T00:00:00",
                        },
                    ], 1)),
                ),
            ):
                resp = client.get("/api/v1/api-keys/apikey_01/logs?page=1&page_size=10")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert len(data["items"]) == 1
            assert data["items"][0]["user_sub"] == "user-A"
            assert data["items"][0]["total_tokens"] == 100
        finally:
            cleanup()

    def test_logs_key_not_found(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.api.v1.api_keys.ApiKeyService.get_api_key",
                new=AsyncMock(return_value=None),
            ):
                resp = client.get("/api/v1/api-keys/nonexistent/logs")
            assert resp.status_code == 404
        finally:
            cleanup()


class TestApiKeyUsersEndpoint:
    """GET /api/v1/api-keys/{id}/users"""

    def test_users_returns_ranked_list(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            with (
                patch(
                    "app.api.v1.api_keys.ApiKeyService.get_api_key",
                    new=AsyncMock(return_value=_make_key_doc()),
                ),
                patch(
                    "app.services.execution_log_service.ExecutionLogService.get_users_summary_by_api_key",
                    new=AsyncMock(return_value=[
                        {"user_sub": "user-A", "calls": 10, "total_tokens": 500, "last_seen_at": "2026-07-21T00:00:00"},
                        {"user_sub": "user-B", "calls": 2, "total_tokens": 100, "last_seen_at": "2026-07-20T00:00:00"},
                    ]),
                ),
            ):
                resp = client.get("/api/v1/api-keys/apikey_01/users?period_days=7")
            assert resp.status_code == 200
            data = resp.json()
            assert data["period_days"] == 7
            assert len(data["items"]) == 2
            assert data["items"][0]["user_sub"] == "user-A"
            assert data["items"][0]["total_tokens"] == 500
        finally:
            cleanup()

    def test_users_key_not_found(self, client, admin_user) -> None:
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.api.v1.api_keys.ApiKeyService.get_api_key",
                new=AsyncMock(return_value=None),
            ):
                resp = client.get("/api/v1/api-keys/nonexistent/users")
            assert resp.status_code == 404
        finally:
            cleanup()
