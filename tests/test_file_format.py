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

# GRIB headers: b"GRIB" + 3 reserved/length bytes + edition byte
GRIB1 = b"GRIB\x00\x00\x00\x01"
GRIB2 = b"GRIB\x00\x00\x00\x02"
GRIB_UNKNOWN_EDITION = b"GRIB\x00\x00\x00\x99"

# BUFR headers share GRIB's layout: b"BUFR" + 3 length bytes + edition byte
BUFR3 = b"BUFR\x00\x00\x00\x03"
BUFR4 = b"BUFR\x00\x00\x00\x04"

HDF4 = b"\x0e\x03\x13\x01" + b"\x00" * 4

TIFF_LE = b"II*\x00" + b"\x00" * 4
TIFF_BE = b"MM\x00*" + b"\x00" * 4
BIGTIFF_LE = b"II+\x00" + b"\x00" * 4
BIGTIFF_BE = b"MM\x00+" + b"\x00" * 4

NOT_RECOGNIZED = b"NOTATHING"


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


def _mock_zarr_http_client(present: set[str]):
    """Build a mock httpx.AsyncClient whose HEAD returns 200 for URLs ending
    in one of `present`, and 404 otherwise."""

    async def head(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200 if any(url.endswith(suffix) for suffix in present) else 404
        return resp

    mock_client = AsyncMock()
    mock_client.head = AsyncMock(side_effect=head)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


# ---------------------------------------------------------------------------
# Local file tests
# ---------------------------------------------------------------------------


class TestLocalFile:
    @pytest.mark.parametrize(
        "magic, expected_family, expected_format",
        [
            (CDF1, "netcdf", "netcdf3-classic"),
            (CDF2, "netcdf", "netcdf3-64bit-offset"),
            (CDF5, "netcdf", "netcdf3-64bit-data"),
            (HDF5, "netcdf", "netcdf4"),
            (GRIB1, "grib", "grib1"),
            (GRIB2, "grib", "grib2"),
            (GRIB_UNKNOWN_EDITION, "grib", "grib153"),
            (BUFR3, "bufr", "bufr3"),
            (BUFR4, "bufr", "bufr4"),
            (HDF4, "hdf", "hdf4"),
            (TIFF_LE, "tiff", "tiff"),
            (TIFF_BE, "tiff", "tiff"),
            (BIGTIFF_LE, "tiff", "bigtiff"),
            (BIGTIFF_BE, "tiff", "bigtiff"),
        ],
    )
    def test_format_variants(self, tmp_path, magic, expected_family, expected_format):
        f = tmp_path / "test.dat"
        f.write_bytes(magic + b"\x00" * 100)

        resp = client.get(f"/files/format?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == expected_family
        assert body["format"] == expected_format
        assert body["path"] == str(f)

    def test_unrecognized_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(NOT_RECOGNIZED + b"\x00" * 100)

        resp = client.get(f"/files/format?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] is None
        assert body["format"] is None

    def test_missing_file_returns_404(self):
        resp = client.get("/files/format?path=/nonexistent/file.dat")

        assert resp.status_code == 404
        assert "File not found" in resp.json()["detail"]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.dat"
        f.write_bytes(b"")

        resp = client.get(f"/files/format?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] is None
        assert body["format"] is None


# ---------------------------------------------------------------------------
# URL tests
# ---------------------------------------------------------------------------


class TestURL:
    @pytest.mark.parametrize(
        "magic, expected_family, expected_format",
        [
            (CDF1, "netcdf", "netcdf3-classic"),
            (HDF5, "netcdf", "netcdf4"),
            (GRIB1, "grib", "grib1"),
            (GRIB2, "grib", "grib2"),
            (BUFR4, "bufr", "bufr4"),
            (HDF4, "hdf", "hdf4"),
            (TIFF_LE, "tiff", "tiff"),
            (BIGTIFF_LE, "tiff", "bigtiff"),
        ],
    )
    def test_format_url_variants(self, magic, expected_family, expected_format):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_http_client(magic)):
            resp = client.get("/files/format?path=https://example.com/test.dat")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == expected_family
        assert body["format"] == expected_format

    def test_unrecognized_url(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_http_client(NOT_RECOGNIZED)):
            resp = client.get("/files/format?path=https://example.com/page.html")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] is None
        assert body["format"] is None

    def test_url_http_error_returns_502(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_http_client(b"", status_code=404)):
            resp = client.get("/files/format?path=https://example.com/missing.dat")

        assert resp.status_code == 502
        assert "HTTP 404" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Zarr tests
# ---------------------------------------------------------------------------


class TestZarrLocal:
    def test_zarr_v3(self, tmp_path):
        store = tmp_path / "data.zarr"
        store.mkdir()
        (store / "zarr.json").write_text("{}")

        resp = client.get(f"/files/format?path={store}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == "zarr"
        assert body["format"] == "zarr3"

    @pytest.mark.parametrize("marker", [".zgroup", ".zarray"])
    def test_zarr_v2(self, tmp_path, marker):
        store = tmp_path / "data.zarr"
        store.mkdir()
        (store / marker).write_text("{}")

        resp = client.get(f"/files/format?path={store}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == "zarr"
        assert body["format"] == "zarr2"

    def test_v3_marker_takes_precedence_over_v2(self, tmp_path):
        store = tmp_path / "data.zarr"
        store.mkdir()
        (store / "zarr.json").write_text("{}")
        (store / ".zgroup").write_text("{}")

        resp = client.get(f"/files/format?path={store}")

        assert resp.json()["format"] == "zarr3"

    def test_zarr_named_dir_without_markers_is_unrecognized(self, tmp_path):
        store = tmp_path / "data.zarr"
        store.mkdir()

        resp = client.get(f"/files/format?path={store}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] is None
        assert body["format"] is None

    def test_missing_zarr_path_returns_404(self, tmp_path):
        resp = client.get(f"/files/format?path={tmp_path}/missing.zarr")

        assert resp.status_code == 404

    def test_directory_without_zarr_suffix_is_still_detected(self, tmp_path):
        """The .zarr suffix is a convention, not a spec requirement - any local
        directory should be checked for marker files regardless of its name."""
        store = tmp_path / "my_dataset"
        store.mkdir()
        (store / "zarr.json").write_text("{}")

        resp = client.get(f"/files/format?path={store}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == "zarr"
        assert body["format"] == "zarr3"

    def test_non_zarr_directory_without_suffix_is_unrecognized(self, tmp_path):
        """A plain directory with no marker files and no .zarr suffix should be
        reported as unrecognized (200, nulls) rather than erroring."""
        d = tmp_path / "just_a_folder"
        d.mkdir()
        (d / "notes.txt").write_text("hello")

        resp = client.get(f"/files/format?path={d}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] is None
        assert body["format"] is None


class TestZarrURL:
    def test_zarr_v3(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_zarr_http_client({"zarr.json"})):
            resp = client.get("/files/format?path=https://example.com/data.zarr")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == "zarr"
        assert body["format"] == "zarr3"

    @pytest.mark.parametrize("marker", [".zgroup", ".zarray"])
    def test_zarr_v2(self, marker):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_zarr_http_client({marker})):
            resp = client.get("/files/format?path=https://example.com/data.zarr")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == "zarr"
        assert body["format"] == "zarr2"

    def test_zarr_named_url_without_markers_is_unrecognized(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_zarr_http_client(set())):
            resp = client.get("/files/format?path=https://example.com/data.zarr")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] is None
        assert body["format"] is None

    def test_zarr_url_trailing_slash(self):
        with patch("app.routers.files.httpx.AsyncClient", return_value=_mock_zarr_http_client({"zarr.json"})):
            resp = client.get("/files/format?path=https://example.com/data.zarr/")

        assert resp.status_code == 200
        assert resp.json()["format"] == "zarr3"


# ---------------------------------------------------------------------------
# Formats discovery endpoint
# ---------------------------------------------------------------------------


class TestListFormats:
    def test_lists_known_format_values(self):
        resp = client.get("/files/formats")

        assert resp.status_code == 200
        body = resp.json()

        assert set(body["netcdf"]["values"]) == {
            "netcdf3-classic",
            "netcdf3-64bit-offset",
            "netcdf3-64bit-data",
            "netcdf4",
        }
        assert set(body["grib"]["values"]) == {"grib1", "grib2"}
        assert set(body["bufr"]["values"]) == {"bufr3", "bufr4"}
        assert set(body["hdf"]["values"]) == {"hdf4"}
        assert set(body["tiff"]["values"]) == {"tiff", "bigtiff"}
        assert set(body["zarr"]["values"]) == {"zarr2", "zarr3"}

    def test_format_endpoint_only_returns_values_listed_by_formats_endpoint(self, tmp_path):
        """Every concrete format /format can return for a known magic byte should
        appear in /formats, so callers can validate against a single source of truth."""
        listed = client.get("/files/formats").json()
        known_netcdf = set(listed["netcdf"]["values"])

        f = tmp_path / "test.nc"
        f.write_bytes(HDF5 + b"\x00" * 100)
        resp = client.get(f"/files/format?path={f}")

        assert resp.json()["format"] in known_netcdf
