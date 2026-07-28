"""Tests for UserAuthService — RFC 7662 introspection client.

Covers: success path, active=false (not cached), timeout/5xx/4xx error
branches, fresh cache hit, stale-cache fallback (AC7), and the rule
that inactive tokens are never cached.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.core.errors import AppError
from app.core.user_auth_state import is_introspect_stale, reset_introspect_stale
from app.services.user_auth_service import IntrospectionResult, UserAuthService

USER_INFO_URL = "https://partner.example.com/oauth/introspect"
USER_TOKEN = "test-token-abc"


def _active_payload(sub: str = "user-123") -> dict:
    return {
        "active": True,
        "sub": sub,
        "username": "zhangsan",
        "email": "zs@example.com",
        "exp": 1735689600,
        "attrs": {"dept": "sales"},
    }


def _patch_httpx_with_handler(monkeypatch, handler):
    """Patch httpx.AsyncClient so each `async with` returns a client whose
    .post() delegates to ``handler`` (a sync callable taking an httpx.Request).
    """

    class _StubClient:
        def __init__(self, *args, **kwargs):
            self._transport = httpx.MockTransport(handler)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            # Build a minimal Request and dispatch through MockTransport so
            # the handler sees a realistic request object.
            content = kwargs.get("data")
            req = httpx.Request("POST", url, data=content)
            resp = await self._transport.handle_async_request(req)
            # Attach the request so resp.raise_for_status() works.
            resp.request = req
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)


@pytest.fixture(autouse=True)
def _reset_stale():
    reset_introspect_stale()
    yield
    reset_introspect_stale()


@pytest.fixture(autouse=True)
def _noop_cache(monkeypatch):
    """Default: cache layer is a no-op (always miss, never stores)."""
    monkeypatch.setattr(
        "app.services.user_auth_service.get_cached_introspection",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.user_auth_service.get_stale_introspection",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.user_auth_service.cache_introspection",
        AsyncMock(return_value=None),
    )


class TestIntrospectSuccess:
    async def test_active_token_returns_parsed_result(self, monkeypatch):
        _patch_httpx_with_handler(
            monkeypatch, lambda req: httpx.Response(200, json=_active_payload())
        )
        result = await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)

        assert isinstance(result, IntrospectionResult)
        assert result.active is True
        assert result.sub == "user-123"
        assert result.username == "zhangsan"
        assert result.attrs == {"dept": "sales"}
        assert result.stale is False

    async def test_form_encoded_post_body(self, monkeypatch):
        """Per RFC 7662, request body must be form-encoded `token=...`."""
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = req.content.decode()
            captured["content_type"] = req.headers.get("content-type")
            return httpx.Response(200, json=_active_payload())

        _patch_httpx_with_handler(monkeypatch, handler)
        await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)

        assert captured["body"] == f"token={USER_TOKEN}"
        assert captured["content_type"] == "application/x-www-form-urlencoded"

    async def test_active_result_is_cached(self, monkeypatch):
        cache_spy = AsyncMock(return_value=None)
        with patch(
            "app.services.user_auth_service.cache_introspection", cache_spy
        ):
            _patch_httpx_with_handler(
                monkeypatch, lambda req: httpx.Response(200, json=_active_payload())
            )
            await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)
        cache_spy.assert_awaited_once()
        cached_token, cached_payload = cache_spy.await_args.args
        assert cached_token == USER_TOKEN
        assert cached_payload["sub"] == "user-123"


class TestIntrospectInactive:
    async def test_inactive_token_returns_result_without_caching(self, monkeypatch):
        cache_spy = AsyncMock(return_value=None)
        with patch(
            "app.services.user_auth_service.cache_introspection", cache_spy
        ):
            _patch_httpx_with_handler(
                monkeypatch, lambda req: httpx.Response(200, json={"active": False})
            )
            result = await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)

        assert result.active is False
        cache_spy.assert_not_awaited()  # Inactive tokens MUST NOT be cached.


class TestIntrospectCacheHit:
    async def test_fresh_cache_hit_skips_http(self, monkeypatch):
        http_called = False

        def handler(req):
            nonlocal http_called
            http_called = True
            return httpx.Response(200, json=_active_payload())

        cached = _active_payload()
        cached["stale"] = False
        monkeypatch.setattr(
            "app.services.user_auth_service.get_cached_introspection",
            AsyncMock(return_value=cached),
        )
        _patch_httpx_with_handler(monkeypatch, handler)
        result = await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)

        assert http_called is False
        assert result.active is True
        assert result.stale is False


class TestIntrospectStaleFallback:
    async def test_timeout_uses_stale_and_marks_flag(self, monkeypatch):
        stale_payload = _active_payload()
        stale_payload["stale"] = True
        monkeypatch.setattr(
            "app.services.user_auth_service.get_stale_introspection",
            AsyncMock(return_value=stale_payload),
        )

        def handler(req):
            raise httpx.ReadTimeout("simulated")

        _patch_httpx_with_handler(monkeypatch, handler)
        result = await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)

        assert result.active is True
        assert is_introspect_stale() is True

    async def test_timeout_without_stale_raises_504(self, monkeypatch):
        def handler(req):
            raise httpx.ReadTimeout("simulated")

        _patch_httpx_with_handler(monkeypatch, handler)
        with pytest.raises(AppError) as exc_info:
            await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)
        assert exc_info.value.code == "EXT_USER_SERVICE_TIMEOUT"
        assert exc_info.value.status_code == 504

    async def test_5xx_without_stale_raises_503(self, monkeypatch):
        _patch_httpx_with_handler(
            monkeypatch, lambda req: httpx.Response(503)
        )
        with pytest.raises(AppError) as exc_info:
            await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)
        assert exc_info.value.code == "EXT_USER_SERVICE_UNAVAILABLE"
        assert exc_info.value.status_code == 503

    async def test_4xx_without_stale_raises_503(self, monkeypatch):
        _patch_httpx_with_handler(
            monkeypatch, lambda req: httpx.Response(400)
        )
        with pytest.raises(AppError) as exc_info:
            await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)
        assert exc_info.value.code == "EXT_USER_SERVICE_UNAVAILABLE"
        assert exc_info.value.status_code == 503

    async def test_network_error_uses_stale(self, monkeypatch):
        stale_payload = _active_payload()
        stale_payload["stale"] = True
        monkeypatch.setattr(
            "app.services.user_auth_service.get_stale_introspection",
            AsyncMock(return_value=stale_payload),
        )

        def handler(req):
            raise httpx.ConnectError("boom")

        _patch_httpx_with_handler(monkeypatch, handler)
        result = await UserAuthService.introspect(USER_INFO_URL, USER_TOKEN)

        assert result.active is True
        assert is_introspect_stale() is True
