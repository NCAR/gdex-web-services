from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _mock_head_client(status_code: int = 200, content_length: str | None = "1234"):
    """Build a mock httpx.AsyncClient that returns the given headers on HEAD."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-length": content_length} if content_length is not None else {}

    mock_client = AsyncMock()
    mock_client.head = AsyncMock(return_value=resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


# ---------------------------------------------------------------------------
# Local file/directory tests
# ---------------------------------------------------------------------------


class TestLocalPath:
    def test_file_size(self, tmp_path):
        f = tmp_path / "test.nc"
        f.write_bytes(b"\x00" * 100)

        resp = client.get(f"/files/size?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == str(f)
        assert body["is_directory"] is False
        assert body["size_bytes"] == 100

    def test_directory_size_sums_files_recursively(self, tmp_path):
        (tmp_path / "a.nc").write_bytes(b"\x00" * 50)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.nc").write_bytes(b"\x00" * 25)

        resp = client.get(f"/files/size?path={tmp_path}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_directory"] is True
        assert body["size_bytes"] == 75
    def test_empty_directory_size_is_zero(self, tmp_path):
        resp = client.get(f"/files/size?path={tmp_path}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_directory"] is True
        assert body["size_bytes"] == 0

    def test_missing_path_returns_404(self):
        resp = client.get("/files/size?path=/nonexistent/file.nc")

        assert resp.status_code == 404
        assert "Path not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# URL tests
# ---------------------------------------------------------------------------


class TestURL:
    def test_url_size_from_content_length(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_head_client(content_length="4096")):
            resp = client.get("/files/size?path=https://example.com/test.nc")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_directory"] is False
        assert body["size_bytes"] == 4096

    def test_url_without_content_length_returns_null(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_head_client(content_length=None)):
            resp = client.get("/files/size?path=https://example.com/test.nc")

        assert resp.status_code == 200
        assert resp.json()["size_bytes"] is None

    def test_url_http_error_returns_502(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_head_client(status_code=404)):
            resp = client.get("/files/size?path=https://example.com/missing.nc")

        assert resp.status_code == 502
        assert "HTTP 404" in resp.json()["detail"]
