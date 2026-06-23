from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# NetCDF magic byte headers
CDF1 = b"CDF\x01" + b"\x00" * 4
CDF2 = b"CDF\x02" + b"\x00" * 4
CDF5 = b"CDF\x05" + b"\x00" * 4
HDF5 = b"\x89HDF\r\n\x1a\n"
NOT_NETCDF = b"NOTATHING"


def _mock_http_client(content: bytes, status_code: int = 206):
    """Build a mock httpx.AsyncClient that returns the given bytes on GET."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


# ---------------------------------------------------------------------------
# Local file tests
# ---------------------------------------------------------------------------


class TestLocalFile:
    @pytest.mark.parametrize(
        "magic, expected_format",
        [
            (CDF1, "CDF-1"),
            (CDF2, "CDF-2"),
            (CDF5, "CDF-5"),
            (HDF5, "NetCDF-4"),
        ],
    )
    def test_netcdf_variants(self, tmp_path, magic, expected_format):
        f = tmp_path / "test.nc"
        f.write_bytes(magic + b"\x00" * 100)

        resp = client.get(f"/files/is-netcdf?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_netcdf"] is True
        assert body["format"] == expected_format
        assert body["path"] == str(f)

    def test_non_netcdf_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(NOT_NETCDF + b"\x00" * 100)

        resp = client.get(f"/files/is-netcdf?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_netcdf"] is False
        assert body["format"] is None

    def test_missing_file_returns_404(self):
        resp = client.get("/files/is-netcdf?path=/nonexistent/file.nc")

        assert resp.status_code == 404
        assert "File not found" in resp.json()["detail"]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.nc"
        f.write_bytes(b"")

        resp = client.get(f"/files/is-netcdf?path={f}")

        assert resp.status_code == 200
        assert resp.json()["is_netcdf"] is False


# ---------------------------------------------------------------------------
# URL tests
# ---------------------------------------------------------------------------


class TestURL:
    @pytest.mark.parametrize(
        "magic, expected_format",
        [
            (CDF1, "CDF-1"),
            (CDF2, "CDF-2"),
            (CDF5, "CDF-5"),
            (HDF5, "NetCDF-4"),
        ],
    )
    def test_netcdf_url_variants(self, magic, expected_format):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_http_client(magic)):
            resp = client.get("/files/is-netcdf?path=https://example.com/test.nc")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_netcdf"] is True
        assert body["format"] == expected_format

    def test_non_netcdf_url(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_http_client(NOT_NETCDF)):
            resp = client.get("/files/is-netcdf?path=https://example.com/page.html")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_netcdf"] is False
        assert body["format"] is None

    def test_url_http_error_returns_502(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_http_client(b"", status_code=404)):
            resp = client.get("/files/is-netcdf?path=https://example.com/missing.nc")

        assert resp.status_code == 502
        assert "HTTP 404" in resp.json()["detail"]

    def test_url_full_response_accepted(self):
        """Servers that ignore Range headers return 200 instead of 206; both are valid."""
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_http_client(CDF1, status_code=200)):
            resp = client.get("/files/is-netcdf?path=https://example.com/test.nc")

        assert resp.status_code == 200
        assert resp.json()["is_netcdf"] is True
