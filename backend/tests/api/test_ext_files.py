"""Tests for external API — session file endpoints (ownership + scope)."""
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
    """Legacy-mode principal (user_info_url empty)."""
    return ApiKeyPrincipal(
        key_id="apikey_test",
        owner_user_id="user_owner",
        scopes=["agents:read", "agents:invoke"],
        bindings={"agents": [], "workflows": []},
    )


def _override_auth(principal):
    app.dependency_overrides[auth_and_rate_limit] = lambda: principal
    return lambda: app.dependency_overrides.clear()


def _callback_principal() -> ApiKeyPrincipal:
    """Callback-mode principal already resolved by the auth layer."""
    return ApiKeyPrincipal(
        key_id="apikey_cb",
        owner_user_id="user_owner",
        scopes=["agents:read", "agents:invoke"],
        bindings={"agents": [], "workflows": []},
        user_info_url="https://partner.example.com/introspect",
        user_id="user_owner:user-123",
    )


class TestSessionFileOwnership:
    """An end-user must not reach another end-user's session files."""

    def test_upload_other_user_session_404(self, client, full_principal) -> None:
        cleanup = _override_auth(full_principal)
        try:
            # Session belongs to a different visitor → ownership mismatch.
            with patch(
                "app.services.session_service.SessionService.get_session",
                new=AsyncMock(
                    return_value={"_id": "s1", "user_id": "user_owner:someone-else"}
                ),
            ):
                resp = client.post(
                    "/api/v1/ext/sessions/s1/files/upload?visitor_id=v-abc",
                    files={"file": ("a.txt", b"hello", "text/plain")},
                )
            assert resp.status_code == 404
        finally:
            cleanup()

    def test_list_other_user_session_404(self, client, full_principal) -> None:
        cleanup = _override_auth(full_principal)
        try:
            with patch(
                "app.services.session_service.SessionService.get_session",
                new=AsyncMock(
                    return_value={"_id": "s1", "user_id": "user_owner:someone-else"}
                ),
            ):
                resp = client.get("/api/v1/ext/sessions/s1/files?visitor_id=v-abc")
            assert resp.status_code == 404
        finally:
            cleanup()

    def test_files_scope_denied(self, client) -> None:
        principal = ApiKeyPrincipal(
            key_id="k",
            owner_user_id="u",
            scopes=["agents:read"],  # no agents:invoke
            bindings={},
        )
        cleanup = _override_auth(principal)
        try:
            resp = client.get("/api/v1/ext/sessions/s1/files?visitor_id=v-abc")
            assert resp.status_code == 403
        finally:
            cleanup()


class TestSessionFileSuccess:
    """Both auth modes resolve ownership and reach the workspace."""

    def test_list_legacy_success(self, client, full_principal) -> None:
        cleanup = _override_auth(full_principal)
        try:
            fake_ws = object()
            with patch(
                "app.services.session_service.SessionService.get_session",
                new=AsyncMock(
                    return_value={"_id": "s1", "user_id": "user_owner:v-abc"}
                ),
            ), patch(
                "app.engine.tool.workspace.WorkspaceManager.get_workspace",
                return_value=fake_ws,
            ), patch(
                "app.engine.tool.workspace.WorkspaceManager.list_output_files",
                return_value=[{"path": "out.txt", "size": 5, "modified": 0}],
            ) as mock_list:
                resp = client.get("/api/v1/ext/sessions/s1/files?visitor_id=v-abc")
            assert resp.status_code == 200
            assert resp.json()[0]["path"] == "out.txt"
            # Workspace keyed by the resolved legacy user_id.
            assert mock_list.call_args.args[0] is fake_ws
        finally:
            cleanup()

    def test_list_callback_success(self, client) -> None:
        """Callback mode: ownership keyed by {owner}:{sub}, visitor_id ignored."""
        principal = _callback_principal()
        cleanup = _override_auth(principal)
        try:
            with patch(
                "app.services.session_service.SessionService.get_session",
                new=AsyncMock(
                    return_value={"_id": "s1", "user_id": "user_owner:user-123"}
                ),
            ), patch(
                "app.engine.tool.workspace.WorkspaceManager.list_output_files",
                return_value=[],
            ), patch(
                "app.engine.tool.workspace.WorkspaceManager.get_workspace",
                return_value=object(),
            ) as mock_get_ws:
                # visitor_id deliberately omitted — callback mode ignores it.
                resp = client.get("/api/v1/ext/sessions/s1/files")
            assert resp.status_code == 200
            assert resp.json() == []
            # get_workspace received the sub-based user_id.
            assert mock_get_ws.call_args.args[0] == "user_owner:user-123"
        finally:
            cleanup()
