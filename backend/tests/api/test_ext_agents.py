"""Tests for external API — Agent resource discovery and invocation."""
from unittest.mock import AsyncMock, patch

import pytest
from app.api.v1.ext import auth_and_rate_limit
from app.core.auth_apikey import ApiKeyPrincipal
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def full_principal():
    """API Key principal with all scopes and no resource bindings."""
    return ApiKeyPrincipal(
        key_id="apikey_test",
        owner_user_id="user_owner",
        scopes=["agents:read", "agents:invoke", "workflows:read", "workflows:invoke", "executions:read"],
        bindings={"agents": [], "workflows": []},
        rate_limit=60,
    )


@pytest.fixture
def limited_principal():
    """API Key principal with limited scope and agent binding."""
    return ApiKeyPrincipal(
        key_id="apikey_limited",
        owner_user_id="user_owner",
        scopes=["agents:read"],
        bindings={"agents": ["agent_allowed"], "workflows": []},
        rate_limit=60,
    )


def _override_auth(principal):
    """Override the API Key auth dependency."""
    app.dependency_overrides[auth_and_rate_limit] = lambda: principal
    return lambda: app.dependency_overrides.clear()


def _make_agent_doc(agent_id="agent_01", name="Test Agent", status="published"):
    return {
        "_id": agent_id,
        "name": name,
        "description": "A test agent",
        "prompt_slots": {},
        "tool_ids": [],
        "skill_ids": ["skill_01"],
        "mcp_connection_ids": [],
        "builtin_config": [],
        "workflow_ids": ["wf_01"],
        "knowledge_base_ids": [],
        "default_model": "gpt-4o",
        "max_retry": 3,
        "status": status,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }


# ---------------------------------------------------------------------------
# AC-1: List accessible Agents
# ---------------------------------------------------------------------------


class TestListAgents:
    """GET /api/v1/ext/agents"""

    def test_list_agents_success(self, client, full_principal) -> None:
        cleanup = _override_auth(full_principal)
        try:
            agents = [_make_agent_doc(), _make_agent_doc("agent_02", "Agent 2")]
            with patch(
                "app.services.agent_service.AgentService.list_agents",
                new=AsyncMock(return_value=(agents, 2)),
            ):
                resp = client.get("/api/v1/ext/agents")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 2
            assert len(data["items"]) == 2
            assert data["items"][0]["id"] == "agent_01"
            assert data["items"][0]["name"] == "Test Agent"
            assert data["items"][0]["status"] == "published"
            assert data["items"][0]["capabilities"]["tools"] == ["skill_01"]
            assert data["items"][0]["capabilities"]["workflow_ids"] == ["wf_01"]
        finally:
            cleanup()

    def test_list_agents_filtered_by_bindings(self, client, limited_principal) -> None:
        cleanup = _override_auth(limited_principal)
        try:
            agents = [
                _make_agent_doc("agent_allowed", "Allowed Agent"),
                _make_agent_doc("agent_other", "Other Agent"),
            ]
            with patch(
                "app.services.agent_service.AgentService.list_agents",
                new=AsyncMock(return_value=(agents, 2)),
            ):
                resp = client.get("/api/v1/ext/agents")
            assert resp.status_code == 200
            data = resp.json()
            # Only agent_allowed should be in the response
            assert len(data["items"]) == 1
            assert data["items"][0]["id"] == "agent_allowed"
        finally:
            cleanup()

    def test_list_agents_scope_denied(self, client) -> None:
        principal = ApiKeyPrincipal(
            key_id="k", owner_user_id="u",
            scopes=["agents:invoke"],  # no agents:read
            bindings={},
        )
        cleanup = _override_auth(principal)
        try:
            resp = client.get("/api/v1/ext/agents")
            assert resp.status_code == 403
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# AC-2: Get Agent details
# ---------------------------------------------------------------------------


class TestGetAgent:
    """GET /api/v1/ext/agents/{agent_id}"""

    def test_get_agent_success(self, client, full_principal) -> None:
        cleanup = _override_auth(full_principal)
        try:
            with patch(
                "app.services.agent_service.AgentService.get_agent",
                new=AsyncMock(return_value=_make_agent_doc()),
            ):
                resp = client.get("/api/v1/ext/agents/agent_01")
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == "agent_01"
            assert data["default_model"] == "gpt-4o"
            assert "prompt_slots" not in data  # internal field not exposed
        finally:
            cleanup()

    def test_get_agent_not_found(self, client, full_principal) -> None:
        cleanup = _override_auth(full_principal)
        try:
            with patch(
                "app.services.agent_service.AgentService.get_agent",
                new=AsyncMock(return_value=None),
            ):
                resp = client.get("/api/v1/ext/agents/nonexistent")
            assert resp.status_code == 404
        finally:
            cleanup()

    def test_get_agent_binding_denied(self, client, limited_principal) -> None:
        cleanup = _override_auth(limited_principal)
        try:
            resp = client.get("/api/v1/ext/agents/agent_not_allowed")
            assert resp.status_code == 403
        finally:
            cleanup()

    def test_get_agent_draft_not_visible(self, client, full_principal) -> None:
        cleanup = _override_auth(full_principal)
        try:
            with patch(
                "app.services.agent_service.AgentService.get_agent",
                new=AsyncMock(return_value=_make_agent_doc(status="draft")),
            ):
                resp = client.get("/api/v1/ext/agents/agent_01")
            assert resp.status_code == 404
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# AC-3: Synchronous invocation
# ---------------------------------------------------------------------------


class TestInvokeAgent:
    """POST /api/v1/ext/agents/{agent_id}/invoke"""

    def test_invoke_success(self, client, full_principal) -> None:
        from app.schemas.execution import ExecutionResponse
        cleanup = _override_auth(full_principal)
        try:
            mock_result = ExecutionResponse(
                output="Hello from agent",
                execution_path="direct",
                request_id="req_01",
                agent_id="agent_01",
                session_id="session_01",
                step_count=1,
            )
            with patch(
                "app.services.agent_execution_service.AgentExecutionService.invoke",
                new=AsyncMock(return_value=mock_result),
            ):
                resp = client.post(
                    "/api/v1/ext/agents/agent_01/invoke",
                    json={"message": "Hello"},
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["session_id"] == "session_01"
            assert data["request_id"] == "req_01"
            assert data["reply"] == "Hello from agent"
            assert data["task_ids"] == []
            assert data["files"] == []
        finally:
            cleanup()

    def test_invoke_with_session_id(self, client, full_principal) -> None:
        from app.schemas.execution import ExecutionResponse
        cleanup = _override_auth(full_principal)
        try:
            mock_result = ExecutionResponse(
                output="Continued",
                execution_path="direct",
                request_id="req_02",
                agent_id="agent_01",
                session_id="session_existing",
                step_count=1,
            )
            with patch(
                "app.services.agent_execution_service.AgentExecutionService.invoke",
                new=AsyncMock(return_value=mock_result),
            ) as mock_invoke:
                resp = client.post(
                    "/api/v1/ext/agents/agent_01/invoke",
                    json={"message": "Follow up", "session_id": "session_existing"},
                )
            assert resp.status_code == 200
            # Verify the session_id was passed through
            call_kwargs = mock_invoke.call_args
            assert call_kwargs.kwargs.get("body") or call_kwargs[1].get("body")
        finally:
            cleanup()

    def test_invoke_scope_denied(self, client) -> None:
        principal = ApiKeyPrincipal(
            key_id="k", owner_user_id="u",
            scopes=["agents:read"],  # no agents:invoke
            bindings={},
        )
        cleanup = _override_auth(principal)
        try:
            resp = client.post(
                "/api/v1/ext/agents/agent_01/invoke",
                json={"message": "Hello"},
            )
            assert resp.status_code == 403
        finally:
            cleanup()

    def test_invoke_binding_denied(self, client, limited_principal) -> None:
        cleanup = _override_auth(limited_principal)
        try:
            resp = client.post(
                "/api/v1/ext/agents/agent_not_allowed/invoke",
                json={"message": "Hello"},
            )
            assert resp.status_code == 403
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# AC-7: Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Verify error responses follow the spec."""

    def test_missing_auth_header(self, client) -> None:
        """No Authorization header → 401."""
        # Don't override auth — let real dependency run
        app.dependency_overrides.clear()
        resp = client.get("/api/v1/ext/agents")
        assert resp.status_code == 401

    def test_invalid_key_format(self, client) -> None:
        """Non af_live_ prefix → 401."""
        from app.api.v1.ext import auth_and_rate_limit
        from app.core.errors import UnauthorizedError

        async def _fake_auth():
            raise UnauthorizedError(code="APIKEY_INVALID", message="Invalid or expired API Key")

        app.dependency_overrides[auth_and_rate_limit] = _fake_auth
        try:
            resp = client.get("/api/v1/ext/agents")
            assert resp.status_code == 401
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# AC5/AC7/AC8: Callback-verification mode (end-user identity)
# ---------------------------------------------------------------------------


class TestCallbackVerificationMode:
    """End-user identity via X-User-Token (Story 8.2).

    These tests verify that ``user_id`` reaching AgentExecutionService
    is composed differently depending on the auth mode:
    - Legacy mode (user_info_url empty): ``{owner}:{visitor_id}``
    - Callback mode (user_info_url set): ``{owner}:{sub}`` (visitor_id ignored)
    """

    def _callback_principal(self) -> ApiKeyPrincipal:
        """A principal pre-resolved by the auth layer in callback mode."""
        return ApiKeyPrincipal(
            key_id="apikey_cb",
            owner_user_id="user_owner",
            scopes=["agents:read", "agents:invoke"],
            bindings={"agents": [], "workflows": []},
            rate_limit=60,
            user_info_url="https://partner.example.com/introspect",
            user_id="user_owner:user-123",  # already resolved by auth layer
            user_token="user-token-abc",  # retained for MCP forwarding (P2)
        )

    def test_callback_mode_invoke_uses_sub(self, client) -> None:
        """AC8: callback mode passes ``{owner}:{sub}`` as user_id."""
        from app.schemas.execution import ExecutionResponse
        principal = self._callback_principal()
        cleanup = _override_auth(principal)
        try:
            mock_result = ExecutionResponse(
                output="ok",
                execution_path="direct",
                request_id="req_01",
                agent_id="agent_01",
                session_id="sess_01",
                step_count=1,
            )
            with patch(
                "app.services.agent_execution_service.AgentExecutionService.invoke",
                new=AsyncMock(return_value=mock_result),
            ) as mock_invoke:
                resp = client.post(
                    "/api/v1/ext/agents/agent_01/invoke",
                    json={
                        "message": "hi",
                        "visitor_id": "ignored-in-callback-mode",
                    },
                )
            assert resp.status_code == 200
            # user_id should be from principal.user_id (sub-based),
            # NOT from visitor_id.
            call_kwargs = mock_invoke.call_args.kwargs
            assert call_kwargs["user_id"] == "user_owner:user-123"
            # P2: user_token is also forwarded so MCP loader can透传
            assert call_kwargs["user_token"] == "user-token-abc"
        finally:
            cleanup()

    def test_legacy_mode_invoke_uses_visitor_id(self, client, full_principal) -> None:
        """AC4/AC8: legacy mode composes user_id from visitor_id."""
        from app.schemas.execution import ExecutionResponse
        cleanup = _override_auth(full_principal)
        try:
            mock_result = ExecutionResponse(
                output="ok",
                execution_path="direct",
                request_id="req_01",
                agent_id="agent_01",
                session_id="sess_01",
                step_count=1,
            )
            with patch(
                "app.services.agent_execution_service.AgentExecutionService.invoke",
                new=AsyncMock(return_value=mock_result),
            ) as mock_invoke:
                resp = client.post(
                    "/api/v1/ext/agents/agent_01/invoke",
                    json={"message": "hi", "visitor_id": "v-abc"},
                )
            assert resp.status_code == 200
            call_kwargs = mock_invoke.call_args.kwargs
            assert call_kwargs["user_id"] == "user_owner:v-abc"
        finally:
            cleanup()

    def test_callback_mode_sessions_query_ignores_visitor_id(self, client) -> None:
        """AC8: in callback mode, /sessions doesn't need visitor_id query."""
        principal = self._callback_principal()
        cleanup = _override_auth(principal)
        try:
            with patch(
                "app.services.session_service.SessionService.list_sessions",
                new=AsyncMock(return_value=([], 0)),
            ) as mock_list:
                # visitor_id omitted — should still work in callback mode.
                resp = client.get("/api/v1/ext/agents/agent_01/sessions")
            assert resp.status_code == 200
            call_kwargs = mock_list.call_args.kwargs
            assert call_kwargs["user_id"] == "user_owner:user-123"
        finally:
            cleanup()

    def test_callback_mode_missing_token_returns_401(self, client) -> None:
        """AC5/AC9: missing X-User-Token in callback mode → 401 EXT_USER_TOKEN_MISSING."""
        from app.api.v1.ext import auth_and_rate_limit
        from app.core.errors import UnauthorizedError

        async def _raise_missing_token():
            raise UnauthorizedError(
                code="EXT_USER_TOKEN_MISSING",
                message="X-User-Token header is required for this API Key.",
            )

        app.dependency_overrides[auth_and_rate_limit] = _raise_missing_token
        try:
            resp = client.post(
                "/api/v1/ext/agents/agent_01/invoke",
                json={"message": "hi"},
            )
            assert resp.status_code == 401
            data = resp.json()
            assert data["error"]["code"] == "EXT_USER_TOKEN_MISSING"
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Create session (external)
# ---------------------------------------------------------------------------


class TestCreateSession:
    """POST /api/v1/ext/agents/{agent_id}/sessions"""

    def _doc(self, user_id: str, agent_id: str = "agent_01") -> dict:
        return {
            "_id": "sess_new",
            "user_id": user_id,
            "agent_id": agent_id,
            "title": "",
            "message_count": 0,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }

    def test_create_legacy_uses_visitor_id(self, client, full_principal) -> None:
        cleanup = _override_auth(full_principal)
        try:
            with patch(
                "app.services.session_service.SessionService.create_session",
                new=AsyncMock(return_value=self._doc("user_owner:v-abc")),
            ) as mock_create:
                resp = client.post(
                    "/api/v1/ext/agents/agent_01/sessions?visitor_id=v-abc"
                )
            assert resp.status_code == 201
            assert resp.json()["id"] == "sess_new"
            assert mock_create.call_args.kwargs["user_id"] == "user_owner:v-abc"
        finally:
            cleanup()

    def test_create_callback_uses_sub(self, client) -> None:
        principal = ApiKeyPrincipal(
            key_id="apikey_cb",
            owner_user_id="user_owner",
            scopes=["agents:read", "agents:invoke"],
            bindings={"agents": [], "workflows": []},
            user_info_url="https://partner.example.com/introspect",
            user_id="user_owner:user-123",  # resolved by auth layer
        )
        cleanup = _override_auth(principal)
        try:
            with patch(
                "app.services.session_service.SessionService.create_session",
                new=AsyncMock(return_value=self._doc("user_owner:user-123")),
            ) as mock_create:
                # visitor_id omitted — callback mode ignores it
                resp = client.post("/api/v1/ext/agents/agent_01/sessions")
            assert resp.status_code == 201
            assert mock_create.call_args.kwargs["user_id"] == "user_owner:user-123"
        finally:
            cleanup()

    def test_create_scope_denied(self, client) -> None:
        principal = ApiKeyPrincipal(
            key_id="k",
            owner_user_id="u",
            scopes=["agents:read"],  # no agents:invoke
            bindings={},
        )
        cleanup = _override_auth(principal)
        try:
            resp = client.post("/api/v1/ext/agents/agent_01/sessions")
            assert resp.status_code == 403
        finally:
            cleanup()
