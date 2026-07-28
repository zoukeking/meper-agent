"""Tests for McpConnectionService."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.errors import ConflictError, NotFoundError
from app.models.mcp_connection import ConnectionStatus
from app.services.mcp_connection_service import McpConnectionService


@pytest.fixture(autouse=True)
def mock_database():
    """Mock the MongoDB database."""
    mock_db = MagicMock()
    with patch("app.services.mcp_connection_service.get_database", return_value=mock_db):
        yield mock_db


def _make_find_one_mock(doc=None):
    """Create a mock for find_one that returns the given doc."""
    return AsyncMock(return_value=doc)


@pytest.mark.asyncio
async def test_create_connection_success(mock_database):
    """Test creating an MCP connection."""
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=None)
    mock_col.insert_one = AsyncMock()
    mock_database.__getitem__.return_value = mock_col

    data = {
        "name": "test-conn",
        "url": "http://localhost:8080/mcp",
        "protocol": "streamable-http",
        "auth_type": "none",
        "auth_config": {},
        "timeout": 30,
        "description": "Test connection",
    }
    doc = await McpConnectionService.create_connection(data)

    assert doc["name"] == "test-conn"
    assert doc["url"] == "http://localhost:8080/mcp"
    assert doc["status"] == ConnectionStatus.DISCONNECTED.value
    assert doc["_id"].startswith("mcp_")
    assert doc["protocol"] == "streamable-http"
    mock_col.insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_create_connection_name_conflict(mock_database):
    """Test creating a connection with duplicate name raises ConflictError."""
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value={"_id": "mcp_existing", "name": "dup"})
    mock_database.__getitem__.return_value = mock_col

    with pytest.raises(ConflictError) as exc:
        await McpConnectionService.create_connection({
            "name": "dup",
            "url": "http://localhost:8080/mcp",
        })
    assert "MCP_CONN_NAME_CONFLICT" in str(exc.value.code) or exc.value.code == "MCP_CONN_NAME_CONFLICT"


@pytest.mark.asyncio
async def test_get_connection_found(mock_database):
    """Test getting a connection that exists."""
    expected = {"_id": "mcp_123", "name": "test", "url": "http://test.com"}
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=expected)
    mock_database.__getitem__.return_value = mock_col

    result = await McpConnectionService.get_connection("mcp_123")
    assert result == expected


@pytest.mark.asyncio
async def test_get_connection_not_found(mock_database):
    """Test getting a non-existent connection returns None."""
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=None)
    mock_database.__getitem__.return_value = mock_col

    result = await McpConnectionService.get_connection("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_list_connections(mock_database):
    """Test listing connections with pagination."""
    mock_col = MagicMock()
    mock_col.count_documents = AsyncMock(return_value=2)
    mock_cursor = MagicMock()
    mock_cursor.sort = MagicMock(return_value=mock_cursor)
    mock_cursor.skip = MagicMock(return_value=mock_cursor)
    mock_cursor.limit = MagicMock(return_value=mock_cursor)
    mock_cursor.to_list = AsyncMock(return_value=[
        {"_id": "mcp_1", "name": "conn1", "url": "http://1.com"},
        {"_id": "mcp_2", "name": "conn2", "url": "http://2.com"},
    ])
    mock_col.find = MagicMock(return_value=mock_cursor)
    mock_database.__getitem__.return_value = mock_col

    items, total = await McpConnectionService.list_connections(page=1, page_size=20)
    assert total == 2
    assert len(items) == 2
    assert items[0]["name"] == "conn1"


@pytest.mark.asyncio
async def test_update_connection_success(mock_database):
    """Test updating a connection."""
    existing = {
        "_id": "mcp_123",
        "name": "old-name",
        "url": "http://old.com",
        "protocol": "streamable-http",
        "auth_type": "none",
        "auth_config": {},
        "timeout": 30,
        "description": "",
    }
    updated = {**existing, "name": "new-name", "url": "http://new.com"}

    mock_col = MagicMock()
    # First call: find existing; Second call: check name conflict; Third call: find updated
    mock_col.find_one = AsyncMock(side_effect=[existing, None, updated])
    mock_col.update_one = AsyncMock()
    mock_database.__getitem__.return_value = mock_col

    result = await McpConnectionService.update_connection("mcp_123", {
        "name": "new-name",
        "url": "http://new.com",
    })

    assert result["name"] == "new-name"
    assert result["url"] == "http://new.com"
    mock_col.update_one.assert_called_once()


@pytest.mark.asyncio
async def test_update_connection_not_found(mock_database):
    """Test updating a non-existent connection returns None."""
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=None)
    mock_database.__getitem__.return_value = mock_col

    result = await McpConnectionService.update_connection("nonexistent", {
        "name": "test",
        "url": "http://test.com",
    })
    assert result is None


@pytest.mark.asyncio
async def test_update_connection_name_conflict(mock_database):
    """Test updating connection name to a name already in use."""
    existing = {
        "_id": "mcp_123",
        "name": "old-name",
        "url": "http://test.com",
        "protocol": "streamable-http",
        "auth_type": "none",
        "auth_config": {},
        "timeout": 30,
    }
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(side_effect=[existing, {"_id": "mcp_other", "name": "taken"}])
    mock_database.__getitem__.return_value = mock_col

    with pytest.raises(ConflictError):
        await McpConnectionService.update_connection("mcp_123", {
            "name": "taken",
            "url": "http://test.com",
        })


@pytest.mark.asyncio
async def test_delete_connection_success(mock_database):
    """Test deleting a connection and cascading tool removal."""
    existing = {"_id": "mcp_123", "name": "test"}
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=existing)
    mock_col.delete_one = AsyncMock()

    mock_tools_col = MagicMock()
    mock_delete_result = MagicMock()
    mock_delete_result.deleted_count = 3
    mock_tools_col.delete_many = AsyncMock(return_value=mock_delete_result)

    mock_database.__getitem__.side_effect = lambda key: mock_tools_col if key == "tools" else mock_col

    result = await McpConnectionService.delete_connection("mcp_123")
    assert result is True
    mock_tools_col.delete_many.assert_called_once()
    mock_col.delete_one.assert_called_once()


@pytest.mark.asyncio
async def test_delete_connection_not_found(mock_database):
    """Test deleting a non-existent connection returns False."""
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=None)
    mock_database.__getitem__.return_value = mock_col

    result = await McpConnectionService.delete_connection("nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_test_connection_not_found(mock_database):
    """Test testing a non-existent connection raises NotFoundError."""
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=None)
    mock_database.__getitem__.return_value = mock_col

    with pytest.raises(NotFoundError):
        await McpConnectionService.test_connection("nonexistent")


@pytest.mark.asyncio
async def test_create_connection_with_default_params(mock_database):
    """Test creating a connection with default_params."""
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=None)
    mock_col.insert_one = AsyncMock()
    mock_database.__getitem__.return_value = mock_col

    data = {
        "name": "test-conn",
        "url": "http://localhost:8080/mcp",
        "default_params": {"token": "abc123", "api_key": "xyz"},
    }
    doc = await McpConnectionService.create_connection(data)

    assert doc["default_params"] == {"token": "abc123", "api_key": "xyz"}
    mock_col.insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_create_connection_default_params_defaults_to_empty(mock_database):
    """Test creating a connection without default_params defaults to empty dict."""
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=None)
    mock_col.insert_one = AsyncMock()
    mock_database.__getitem__.return_value = mock_col

    data = {
        "name": "test-conn",
        "url": "http://localhost:8080/mcp",
    }
    doc = await McpConnectionService.create_connection(data)

    assert doc["default_params"] == {}


@pytest.mark.asyncio
async def test_update_connection_with_default_params(mock_database):
    """Test updating default_params on a connection."""
    existing = {
        "_id": "mcp_123",
        "name": "test",
        "url": "http://test.com",
        "protocol": "streamable-http",
        "auth_type": "none",
        "auth_config": {},
        "timeout": 30,
        "description": "",
        "default_params": {"old_key": "old_val"},
    }
    updated = {**existing, "default_params": {"new_key": "new_val"}}

    mock_col = MagicMock()
    # find_one calls: 1) get existing, 2) get_connection after update
    # (no name conflict check since data has no "name" field)
    mock_col.find_one = AsyncMock(side_effect=[existing, updated])
    mock_col.update_one = AsyncMock()
    mock_database.__getitem__.return_value = mock_col

    with patch("app.services.mcp_connection_service._invalidate_mcp_cache"):
        result = await McpConnectionService.update_connection("mcp_123", {
            "default_params": {"new_key": "new_val"},
        })

    assert result["default_params"] == {"new_key": "new_val"}
    mock_col.update_one.assert_called_once()


@pytest.mark.asyncio
async def test_discover_tools_not_found(mock_database):
    """Test discovering tools for a non-existent connection raises NotFoundError."""
    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(return_value=None)
    mock_database.__getitem__.return_value = mock_col

    with pytest.raises(NotFoundError):
        await McpConnectionService.discover_tools("nonexistent")


# ---------------------------------------------------------------------------
# auth_config field-level merge (protects secrets from masked round-trips)
# ---------------------------------------------------------------------------

from app.services.mcp_connection_service import (  # noqa: E402
    _MASKED_VALUE,
    _merge_auth_config,
)


def test_merge_auth_config_skips_masked_value():
    """A masked '***' value (redacted in API responses) must not overwrite the
    real secret when round-tripped through an edit form."""
    existing = {"token": "real-secret", "username": "admin"}
    incoming = {"token": _MASKED_VALUE}  # user didn't change the token field

    merged = _merge_auth_config(existing, incoming)
    assert merged["token"] == "real-secret"
    assert merged["username"] == "admin"


def test_merge_auth_config_applies_real_change():
    """A genuinely new value overwrites the old one; untouched keys persist."""
    existing = {"token": "old", "username": "admin"}
    incoming = {"token": "new-token"}

    merged = _merge_auth_config(existing, incoming)
    assert merged["token"] == "new-token"
    assert merged["username"] == "admin"


def test_merge_auth_config_skips_empty_string():
    """Empty strings (e.g. blank input fields) are treated as 'unchanged'."""
    existing = {"token": "real-secret"}
    incoming = {"token": ""}

    merged = _merge_auth_config(existing, incoming)
    assert merged["token"] == "real-secret"


def test_merge_auth_config_none_incoming_keeps_all():
    """When the update omits auth_config entirely, everything is preserved."""
    existing = {"token": "real-secret", "username": "admin"}

    merged = _merge_auth_config(existing, None)
    assert merged == {"token": "real-secret", "username": "admin"}


@pytest.mark.asyncio
async def test_update_connection_preserves_secret_through_masked_round_trip(mock_database):
    """End-to-end regression: editing a connection without changing its secret
    must NOT overwrite the stored secret with the masked '***' placeholder.

    The masked value comes from the list/detail API response; if an edit form
    submits it back verbatim, the real secret would be destroyed. The update
    path must decrypt → merge (skipping ***) → re-encrypt so the secret survives.
    """
    # existing auth_config is stored ENCRYPTED (ciphertext). _decrypt_auth_config
    # reverses it; we mock the crypto to make the test deterministic.
    existing = {
        "_id": "mcp_123",
        "name": "test",
        "url": "http://test.com",
        "protocol": "streamable-http",
        "auth_type": "bearer_token",
        "auth_config": {"token": "ENC(real-secret)"},  # ciphertext in DB
        "timeout": 30,
        "description": "",
    }
    # After update, get_connection returns the merged doc.
    updated = {**existing, "auth_config": {"token": "ENC(real-secret)"}}

    mock_col = MagicMock()
    mock_col.find_one = AsyncMock(side_effect=[existing, updated])
    mock_col.update_one = AsyncMock()
    mock_database.__getitem__.return_value = mock_col

    crypto_path = "app.services.mcp_connection_service"
    with (
        patch(f"{crypto_path}._invalidate_mcp_cache"),
        patch(
            f"{crypto_path}._decrypt_auth_config",
            side_effect=lambda c: {"token": "real-secret"} if c else c,
        ) as mock_decrypt,
        patch(
            f"{crypto_path}._encrypt_auth_config",
            side_effect=lambda c: {k: f"ENC({v})" for k, v in c.items()},
        ) as mock_encrypt,
    ):
        # Frontend submits the masked value back (user didn't edit the token).
        result = await McpConnectionService.update_connection("mcp_123", {
            "auth_config": {"token": "***"},
        })

    # 1. Decrypt ran at least once on the stored ciphertext (update merge, plus
    #    get_connection may call it again) → confirms existing secret was read.
    assert mock_decrypt.called
    # 2. Encrypt received the REAL secret (not "***"), proving the merge kept it.
    encrypted_arg = mock_encrypt.call_args.args[0]
    assert encrypted_arg == {"token": "real-secret"}, (
        f"merge should preserve real secret, got {encrypted_arg}"
    )
    # 3. get_connection decrypts on read → the returned token is the real
    #    secret, NOT the "***" placeholder that was submitted.
    assert result["auth_config"]["token"] == "real-secret"
