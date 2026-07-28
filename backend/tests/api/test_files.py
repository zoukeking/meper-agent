"""API tests for /api/v1/files endpoints (mock-based).

Uses ``unittest.mock`` to mock FileService so tests run without MongoDB.
"""
from unittest.mock import AsyncMock

import pytest
from app.core.security import get_current_user
from app.main import app
from app.models.file_library import FileConsumerKind, FileRef, FileUsage
from app.schemas.user import UserResponse, UserStatus
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_admin():
    user = UserResponse(
        id="user_01HTEST",
        username="admin",
        email="admin@example.com",
        role="admin",
        status=UserStatus.ACTIVE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        permissions=[],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.clear()


@pytest.fixture
def auth_other():
    """A different user for ownership tests."""
    user = UserResponse(
        id="user_02HTEST",
        username="other",
        email="other@example.com",
        role="developer",
        status=UserStatus.ACTIVE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        permissions=[],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.clear()


def _fake_file_ref(
    file_id: str = "file_01HTEST",
    owner_user_id: str = "user_01HTEST",
    name: str = "test.txt",
    status: str = "active",
) -> FileRef:
    return FileRef(
        id=file_id,
        owner_user_id=owner_user_id,
        storage_key=f"{owner_user_id}/files/{file_id}",
        name=name,
        size=100,
        mime_type="text/plain",
        sha256="abc123",
        origin_kind=FileConsumerKind.USER_LIBRARY,
        origin_id=owner_user_id,
        status=status,
    )


def _fake_file_usage(
    file_id: str = "file_01HTEST",
    consumer_kind: FileConsumerKind = FileConsumerKind.USER_LIBRARY,
    consumer_id: str = "user_01HTEST",
) -> FileUsage:
    return FileUsage(
        file_id=file_id,
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
    )


# ---------------------------------------------------------------------------
# POST /files — upload
# ---------------------------------------------------------------------------


class TestUploadFile:
    """POST /api/v1/files."""

    def test_upload_success(self, client: TestClient, auth_admin):
        fake_ref = _fake_file_ref()
        fake_usage = _fake_file_usage()

        from app.api.v1.files import get_file_service

        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=fake_ref)
        mock_svc.add_usage = AsyncMock(return_value=fake_usage)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.post(
            "/api/v1/files",
            files={"file": ("test.txt", b"hello world", "text/plain")},
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["name"] == "test.txt"
        mock_svc.create.assert_called_once()
        mock_svc.add_usage.assert_called_once()
        app.dependency_overrides.clear()

    def test_upload_file_too_large(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        mock_svc = AsyncMock()
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        # Create data > 50MB
        large_data = b"x" * (50 * 1024 * 1024 + 1)
        resp = client.post(
            "/api/v1/files",
            files={"file": ("big.bin", large_data, "application/octet-stream")},
        )

        assert resp.status_code == 413
        app.dependency_overrides.clear()

    def test_upload_unauthenticated(self, client: TestClient):
        # Clear all overrides to ensure no auth
        app.dependency_overrides.clear()
        resp = client.post(
            "/api/v1/files",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /files — list
# ---------------------------------------------------------------------------


class TestListFiles:
    """GET /api/v1/files."""

    def test_list_returns_files(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref()
        mock_svc = AsyncMock()
        mock_svc.list_by_owner = AsyncMock(return_value=([fake_ref], 1))
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "test.txt"
        mock_svc.list_by_owner.assert_called_once_with(
            owner_user_id="user_01HTEST", page=1, page_size=20, status="active"
        )
        app.dependency_overrides.clear()

    def test_list_empty(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        mock_svc = AsyncMock()
        mock_svc.list_by_owner = AsyncMock(return_value=([], 0))
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []
        app.dependency_overrides.clear()

    def test_list_with_status_filter(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        mock_svc = AsyncMock()
        mock_svc.list_by_owner = AsyncMock(return_value=([], 0))
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files?status=trashed")

        assert resp.status_code == 200
        mock_svc.list_by_owner.assert_called_once_with(
            owner_user_id="user_01HTEST", page=1, page_size=20, status="trashed"
        )
        app.dependency_overrides.clear()

    def test_list_with_pagination(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        mock_svc = AsyncMock()
        mock_svc.list_by_owner = AsyncMock(return_value=([], 0))
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files?page=2&page_size=10")

        assert resp.status_code == 200
        mock_svc.list_by_owner.assert_called_once_with(
            owner_user_id="user_01HTEST", page=2, page_size=10, status="active"
        )
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /files/{file_id} — detail
# ---------------------------------------------------------------------------


class TestGetFile:
    """GET /api/v1/files/{file_id}."""

    def test_get_file_success(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref()
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_01HTEST")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "test.txt"
        app.dependency_overrides.clear()

    def test_get_file_not_found(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=None)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_NONEXIST")

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_get_file_not_owner(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        # File belongs to a different user
        fake_ref = _fake_file_ref(owner_user_id="user_OTHER")
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_01HTEST")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /files/{file_id}/download
# ---------------------------------------------------------------------------


class TestDownloadFile:
    """GET /api/v1/files/{file_id}/download."""

    def test_download_success(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref()
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        mock_svc._storage = AsyncMock()
        mock_svc._storage.load = AsyncMock(return_value=b"file content here")
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_01HTEST/download")

        assert resp.status_code == 200, resp.text
        assert resp.content == b"file content here"
        assert resp.headers["content-type"].startswith("text/plain")
        assert 'attachment; filename="test.txt"' in resp.headers["content-disposition"]
        app.dependency_overrides.clear()

    def test_download_chinese_filename(self, client: TestClient, auth_admin):
        """中文文件名下载不应 500(回归 UnicodeEncodeError, request_id cc150f95)。"""
        from urllib.parse import unquote

        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref(name="admin出差申请单.txt")
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        mock_svc._storage = AsyncMock()
        mock_svc._storage.load = AsyncMock(return_value=b"file content")
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_01HTEST/download")

        assert resp.status_code == 200, resp.text
        assert resp.content == b"file content"
        cd = resp.headers["content-disposition"]
        # 必须给出 RFC 5987 的 filename*,且能还原原始中文文件名
        assert "filename*=UTF-8''" in cd
        encoded = cd.split("filename*=UTF-8''", 1)[1]
        assert unquote(encoded, encoding="utf-8") == "admin出差申请单.txt"
        app.dependency_overrides.clear()

    def test_download_not_found(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=None)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_NONEXIST/download")

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_download_content_missing(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref()
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        mock_svc._storage = AsyncMock()
        mock_svc._storage.load = AsyncMock(side_effect=FileNotFoundError("missing"))
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_01HTEST/download")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# DELETE /files/{file_id}
# ---------------------------------------------------------------------------


class TestDeleteFile:
    """DELETE /api/v1/files/{file_id}."""

    def test_soft_delete(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref(status="trashed")
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(side_effect=[
            _fake_file_ref(status="active"),  # _get_owner_file check
            fake_ref,  # return updated ref
        ])
        mock_svc.delete = AsyncMock(return_value=True)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.delete("/api/v1/files/file_01HTEST")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "trashed"
        mock_svc.delete.assert_called_once_with("file_01HTEST", force=False)
        app.dependency_overrides.clear()

    def test_hard_delete_no_references(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref()
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        mock_svc.delete = AsyncMock(return_value=True)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.delete("/api/v1/files/file_01HTEST?force=true")

        assert resp.status_code == 204
        mock_svc.delete.assert_called_once_with("file_01HTEST", force=True)
        app.dependency_overrides.clear()

    def test_hard_delete_has_references_cascades(self, client: TestClient, auth_admin):
        """force=true 时有引用也级联删除，不再 409。"""
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref()
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        mock_svc.delete = AsyncMock(return_value=True)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.delete("/api/v1/files/file_01HTEST?force=true")

        assert resp.status_code == 204
        mock_svc.delete.assert_called_once_with("file_01HTEST", force=True)
        app.dependency_overrides.clear()

    def test_delete_not_owner(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref(owner_user_id="user_OTHER")
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.delete("/api/v1/files/file_01HTEST")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /files/{file_id}/usages
# ---------------------------------------------------------------------------


class TestListFileUsages:
    """GET /api/v1/files/{file_id}/usages."""

    def test_list_usages_success(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref()
        fake_usage = _fake_file_usage()
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        mock_svc.list_usages = AsyncMock(return_value=[fake_usage])
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_01HTEST/usages")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data) == 1
        assert data[0]["file_id"] == "file_01HTEST"
        app.dependency_overrides.clear()

    def test_list_usages_empty(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref()
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        mock_svc.list_usages = AsyncMock(return_value=[])
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_01HTEST/usages")

        assert resp.status_code == 200
        data = resp.json()
        assert data == []
        app.dependency_overrides.clear()

    def test_list_usages_not_owner(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        fake_ref = _fake_file_ref(owner_user_id="user_OTHER")
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=fake_ref)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.get("/api/v1/files/file_01HTEST/usages")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /files/{file_id}/restore
# ---------------------------------------------------------------------------


class TestRestoreFile:
    """POST /api/v1/files/{file_id}/restore."""

    def test_restore_trashed_file(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        active_ref = _fake_file_ref(status="active")
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(side_effect=[
            _fake_file_ref(status="trashed"),  # _get_owner_file check
            active_ref,  # return updated ref
        ])
        mock_svc.update_status = AsyncMock(return_value=True)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.post("/api/v1/files/file_01HTEST/restore")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "active"
        mock_svc.update_status.assert_called_once_with("file_01HTEST", "active")
        app.dependency_overrides.clear()

    def test_restore_already_active_file(self, client: TestClient, auth_admin):
        """active 文件直接返回，无需恢复。"""
        from app.api.v1.files import get_file_service

        active_ref = _fake_file_ref(status="active")
        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=active_ref)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.post("/api/v1/files/file_01HTEST/restore")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        mock_svc.update_status.assert_not_called()
        app.dependency_overrides.clear()

    def test_restore_not_found(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        mock_svc = AsyncMock()
        mock_svc.get = AsyncMock(return_value=None)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.post("/api/v1/files/file_NONEXIST/restore")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /files/trash/empty
# ---------------------------------------------------------------------------


class TestEmptyTrash:
    """POST /api/v1/files/trash/empty."""

    def test_empty_trash_deletes_unref_files(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        trashed_ref = _fake_file_ref(status="trashed")
        mock_svc = AsyncMock()
        mock_svc.list_by_owner = AsyncMock(return_value=([trashed_ref], 1))
        mock_svc.has_usages = AsyncMock(return_value=False)
        mock_svc.delete = AsyncMock(return_value=True)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.post("/api/v1/files/trash/empty")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["deleted_count"] == 1
        mock_svc.delete.assert_called_once_with("file_01HTEST", force=True)
        app.dependency_overrides.clear()

    def test_empty_trash_keeps_referenced_files(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        trashed_ref = _fake_file_ref(status="trashed")
        mock_svc = AsyncMock()
        mock_svc.list_by_owner = AsyncMock(return_value=([trashed_ref], 1))
        mock_svc.has_usages = AsyncMock(return_value=True)
        mock_svc.delete = AsyncMock()
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.post("/api/v1/files/trash/empty")

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_count"] == 0
        mock_svc.delete.assert_not_called()
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /files/cleanup
# ---------------------------------------------------------------------------


class TestCleanupExpiredUsages:
    """POST /api/v1/files/cleanup."""

    def test_cleanup_returns_count(self, client: TestClient, auth_admin):
        from app.api.v1.files import get_file_service

        mock_svc = AsyncMock()
        mock_svc.cleanup_expired_usages = AsyncMock(return_value=5)
        app.dependency_overrides[get_file_service] = lambda: mock_svc

        resp = client.post("/api/v1/files/cleanup")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["deleted_count"] == 5
        mock_svc.cleanup_expired_usages.assert_called_once()
        app.dependency_overrides.clear()
