"""Tests for API Key generation, verification, and auth principal."""

from unittest.mock import AsyncMock

import pytest
from app.core.auth_apikey import ApiKeyPrincipal, get_api_key_principal
from app.core.errors import AppError, ForbiddenError, UnauthorizedError
from app.services.api_key_service import (
    _extract_prefix,
    _generate_raw_key,
    _hash_key,
    _is_key_valid,
    _make_full_key,
    _verify_key,
)


class TestKeyGeneration:
    """Test API Key generation utilities."""

    def test_generate_raw_key_length(self) -> None:
        """Raw key is at least 32 characters."""
        raw = _generate_raw_key()
        assert len(raw) >= 32

    def test_make_full_key_prefix(self) -> None:
        """Full key starts with af_live_."""
        raw = _generate_raw_key()
        full = _make_full_key(raw)
        assert full.startswith("af_live_")

    def test_extract_prefix_length(self) -> None:
        """Prefix is first 12 characters."""
        full = "af_live_abcdefghijklmnop"
        assert _extract_prefix(full) == "af_live_abcd"

    def test_hash_and_verify(self) -> None:
        """Hashed key can be verified correctly."""
        raw = _generate_raw_key()
        full = _make_full_key(raw)
        hashed = _hash_key(full)
        assert _verify_key(full, hashed) is True

    def test_verify_wrong_key_fails(self) -> None:
        """Verification fails for a different key."""
        raw1 = _generate_raw_key()
        full1 = _make_full_key(raw1)
        hashed = _hash_key(full1)

        raw2 = _generate_raw_key()
        full2 = _make_full_key(raw2)
        assert _verify_key(full2, hashed) is False

    def test_different_keys_different_hashes(self) -> None:
        """Same key produces different hashes due to bcrypt salt."""
        raw = _generate_raw_key()
        full = _make_full_key(raw)
        h1 = _hash_key(full)
        h2 = _hash_key(full)
        assert h1 != h2
        # But both verify
        assert _verify_key(full, h1) is True
        assert _verify_key(full, h2) is True


class TestIsKeyValid:
    """Test API Key validity check."""

    def test_active_no_expiry_is_valid(self) -> None:
        doc = {"status": "active", "expires_at": None}
        assert _is_key_valid(doc) is True

    def test_revoked_is_invalid(self) -> None:
        doc = {"status": "revoked", "expires_at": None}
        assert _is_key_valid(doc) is False

    def test_future_expiry_is_valid(self) -> None:
        doc = {"status": "active", "expires_at": "2099-01-01T00:00:00Z"}
        assert _is_key_valid(doc) is True

    def test_past_expiry_is_invalid(self) -> None:
        doc = {"status": "active", "expires_at": "2020-01-01T00:00:00Z"}
        assert _is_key_valid(doc) is False

    def test_invalid_expiry_format_is_invalid(self) -> None:
        doc = {"status": "active", "expires_at": "not-a-date"}
        assert _is_key_valid(doc) is False


class TestApiKeyPrincipal:
    """Test the ApiKeyPrincipal authorization logic."""

    def test_has_scope(self) -> None:
        p = ApiKeyPrincipal(
            key_id="k1",
            owner_user_id="u1",
            scopes=["agents:read", "agents:invoke"],
        )
        assert p.has_scope("agents:read") is True
        assert p.has_scope("workflows:invoke") is False

    def test_require_scope_passes(self) -> None:
        p = ApiKeyPrincipal(key_id="k1", owner_user_id="u1", scopes=["agents:read"])
        p.require_scope("agents:read")  # should not raise

    def test_require_scope_raises(self) -> None:
        p = ApiKeyPrincipal(key_id="k1", owner_user_id="u1", scopes=["agents:read"])
        with pytest.raises(ForbiddenError) as exc:
            p.require_scope("agents:invoke")
        assert exc.value.code == "APIKEY_SCOPE_DENIED"

    def test_can_access_agent_with_binding(self) -> None:
        p = ApiKeyPrincipal(
            key_id="k1",
            owner_user_id="u1",
            bindings={"agents": ["agent_01"], "workflows": []},
        )
        assert p.can_access_agent("agent_01") is True
        assert p.can_access_agent("agent_02") is False

    def test_can_access_agent_empty_means_all(self) -> None:
        p = ApiKeyPrincipal(
            key_id="k1",
            owner_user_id="u1",
            bindings={"agents": [], "workflows": []},
        )
        assert p.can_access_agent("any_agent") is True

    def test_can_access_workflow_with_binding(self) -> None:
        p = ApiKeyPrincipal(
            key_id="k1",
            owner_user_id="u1",
            bindings={"agents": [], "workflows": ["wf_01"]},
        )
        assert p.can_access_workflow("wf_01") is True
        assert p.can_access_workflow("wf_02") is False

    def test_can_access_workflow_empty_means_all(self) -> None:
        p = ApiKeyPrincipal(
            key_id="k1",
            owner_user_id="u1",
            bindings={"agents": [], "workflows": []},
        )
        assert p.can_access_workflow("any_wf") is True

    def test_require_agent_access_raises(self) -> None:
        p = ApiKeyPrincipal(
            key_id="k1",
            owner_user_id="u1",
            bindings={"agents": ["agent_01"], "workflows": []},
        )
        with pytest.raises(ForbiddenError) as exc:
            p.require_agent_access("agent_02")
        assert exc.value.code == "APIKEY_AGENT_DENIED"

    def test_require_workflow_access_raises(self) -> None:
        p = ApiKeyPrincipal(
            key_id="k1",
            owner_user_id="u1",
            bindings={"agents": [], "workflows": ["wf_01"]},
        )
        with pytest.raises(ForbiddenError) as exc:
            p.require_workflow_access("wf_02")
        assert exc.value.code == "APIKEY_WORKFLOW_DENIED"


class TestGetApiKeyPrincipal:
    """End-to-end tests for the ``get_api_key_principal`` dependency.

    Covers both legacy mode (``user_info_url`` empty) and callback-verification
    mode (``user_info_url`` set), plus all error branches added in Story 8.2.
    """

    @pytest.fixture
    def legacy_doc(self):
        return {
            "_id": "apikey_01",
            "owner_user_id": "user_owner",
            "scopes": ["agents:invoke"],
            "bindings": {"agents": [], "workflows": []},
            "rate_limit": 60,
            "user_info_url": "",
        }

    @pytest.fixture
    def callback_doc(self):
        return {
            "_id": "apikey_02",
            "owner_user_id": "user_owner",
            "scopes": ["agents:invoke"],
            "bindings": {"agents": [], "workflows": []},
            "rate_limit": 60,
            "user_info_url": "https://partner.example.com/introspect",
        }

    def _make_request(self, headers: dict | None = None):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/ext/agents/x/invoke",
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1"))
                for k, v in (headers or {}).items()
            ],
            "query_string": b"",
        }
        return Request(scope)

    async def test_legacy_mode_user_id_stays_none(self, monkeypatch, legacy_doc):
        """AC4: legacy mode does not touch user_id; route layer composes it."""
        from app.services.api_key_service import ApiKeyService

        monkeypatch.setattr(
            ApiKeyService, "verify_key", AsyncMock(return_value=legacy_doc)
        )
        # Even if X-User-Token is present, legacy mode ignores it.
        request = self._make_request({"X-User-Token": "Bearer some-token"})

        principal = await get_api_key_principal(
            request, authorization="Bearer af_live_test"
        )

        assert principal.user_info_url == ""
        assert principal.user_id is None  # Route layer composes from visitor_id.

    async def test_callback_mode_resolves_user_id(self, monkeypatch, callback_doc):
        """AC5: callback mode resolves user_id from introspection."""
        from app.services.api_key_service import ApiKeyService
        from app.services.user_auth_service import IntrospectionResult

        monkeypatch.setattr(
            ApiKeyService, "verify_key", AsyncMock(return_value=callback_doc)
        )
        monkeypatch.setattr(
            "app.services.user_auth_service.UserAuthService.introspect",
            AsyncMock(
                return_value=IntrospectionResult(
                    active=True, sub="user-123", username="zhangsan"
                )
            ),
        )
        request = self._make_request({"X-User-Token": "Bearer abc"})

        principal = await get_api_key_principal(
            request, authorization="Bearer af_live_test"
        )

        assert principal.user_info_url == callback_doc["user_info_url"]
        assert principal.user_id == "user_owner:user-123"

    async def test_callback_mode_missing_token_raises(self, monkeypatch, callback_doc):
        """AC5: missing X-User-Token in callback mode → EXT_USER_TOKEN_MISSING."""
        from app.services.api_key_service import ApiKeyService

        monkeypatch.setattr(
            ApiKeyService, "verify_key", AsyncMock(return_value=callback_doc)
        )
        request = self._make_request({})

        with pytest.raises(UnauthorizedError) as exc:
            await get_api_key_principal(request, authorization="Bearer af_live_test")
        assert exc.value.code == "EXT_USER_TOKEN_MISSING"

    async def test_callback_mode_invalid_token_raises(self, monkeypatch, callback_doc):
        """AC5: introspection active=false → EXT_USER_TOKEN_INVALID."""
        from app.services.api_key_service import ApiKeyService
        from app.services.user_auth_service import IntrospectionResult

        monkeypatch.setattr(
            ApiKeyService, "verify_key", AsyncMock(return_value=callback_doc)
        )
        monkeypatch.setattr(
            "app.services.user_auth_service.UserAuthService.introspect",
            AsyncMock(return_value=IntrospectionResult(active=False)),
        )
        request = self._make_request({"X-User-Token": "Bearer expired"})

        with pytest.raises(UnauthorizedError) as exc:
            await get_api_key_principal(request, authorization="Bearer af_live_test")
        assert exc.value.code == "EXT_USER_TOKEN_INVALID"

    async def test_callback_mode_missing_sub_raises(self, monkeypatch, callback_doc):
        """AC5: introspection returned active=true but no sub → EXT_USER_TOKEN_INVALID."""
        from app.services.api_key_service import ApiKeyService
        from app.services.user_auth_service import IntrospectionResult

        monkeypatch.setattr(
            ApiKeyService, "verify_key", AsyncMock(return_value=callback_doc)
        )
        monkeypatch.setattr(
            "app.services.user_auth_service.UserAuthService.introspect",
            AsyncMock(return_value=IntrospectionResult(active=True, sub="")),
        )
        request = self._make_request({"X-User-Token": "Bearer weird"})

        with pytest.raises(UnauthorizedError) as exc:
            await get_api_key_principal(request, authorization="Bearer af_live_test")
        assert exc.value.code == "EXT_USER_TOKEN_INVALID"

    async def test_callback_mode_service_unavailable_propagates(
        self, monkeypatch, callback_doc
    ):
        """AC7: AppError(503/504) from introspection propagates unchanged."""
        from app.services.api_key_service import ApiKeyService

        monkeypatch.setattr(
            ApiKeyService, "verify_key", AsyncMock(return_value=callback_doc)
        )

        async def _raise(*_):
            raise AppError(
                code="EXT_USER_SERVICE_UNAVAILABLE",
                message="down",
                status_code=503,
            )

        monkeypatch.setattr(
            "app.services.user_auth_service.UserAuthService.introspect",
            _raise,
        )
        request = self._make_request({"X-User-Token": "Bearer abc"})

        with pytest.raises(AppError) as exc:
            await get_api_key_principal(request, authorization="Bearer af_live_test")
        assert exc.value.code == "EXT_USER_SERVICE_UNAVAILABLE"
        assert exc.value.status_code == 503
