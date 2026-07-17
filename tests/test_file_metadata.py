import re

import cfgrib
import eccodes
import numpy as np
import xarray as xr
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

HDF4 = b"\x0e\x03\x13\x01" + b"\x00" * 4
NOT_RECOGNIZED = b"NOTATHING"


def _write_netcdf(path):
    ds = xr.Dataset(
        {"temperature": (("time",), [1.0, 2.0, 3.0])},
        coords={"time": [0, 1, 2]},
        attrs={"title": "test dataset"},
    )
    ds.to_netcdf(path)
    return ds


def _grib_message(short_name, type_of_level, level):
    """Build a single eccodes GRIB2 message handle on a tiny 4x3 regular lat/lon grid."""
    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    eccodes.codes_set(gid, "shortName", short_name)
    eccodes.codes_set(gid, "typeOfLevel", type_of_level)
    eccodes.codes_set(gid, "level", level)
    eccodes.codes_set(gid, "Ni", 4)
    eccodes.codes_set(gid, "Nj", 3)
    eccodes.codes_set(gid, "latitudeOfFirstGridPointInDegrees", 10.0)
    eccodes.codes_set(gid, "longitudeOfFirstGridPointInDegrees", 0.0)
    eccodes.codes_set(gid, "latitudeOfLastGridPointInDegrees", 0.0)
    eccodes.codes_set(gid, "longitudeOfLastGridPointInDegrees", 30.0)
    eccodes.codes_set(gid, "iDirectionIncrementInDegrees", 10.0)
    eccodes.codes_set(gid, "jDirectionIncrementInDegrees", 5.0)
    eccodes.codes_set_values(gid, np.arange(12, dtype="float64"))
    return gid


def _write_grib(path, messages):
    """Write a GRIB2 file with one message per (shortName, typeOfLevel, level) tuple in `messages`."""
    with open(path, "wb") as f:
        for short_name, type_of_level, level in messages:
            gid = _grib_message(short_name, type_of_level, level)
            eccodes.codes_write(gid, f)
            eccodes.codes_release(gid)


class TestLocalNetcdf:
    def test_netcdf_returns_dataset_str(self, tmp_path):
        f = tmp_path / "test.nc"
        _write_netcdf(f)
        with xr.open_dataset(f) as reopened:
            expected = str(reopened)

        resp = client.get(f"/files/metadata?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == str(f)
        assert body["family"] == "netcdf"
        assert body["format"] == "netcdf4"
        assert body["metadata"] == expected
        assert "temperature" in body["metadata"]


class TestLocalGrib:
    def test_single_hypercube_returns_dataset_str(self, tmp_path):
        f = tmp_path / "test.grib2"
        _write_grib(f, [("2t", "heightAboveGround", 2)])
        [expected_ds] = cfgrib.open_datasets(f)
        expected = f"--- hypercube 1/1 ---\n{expected_ds}"
        expected_ds.close()

        resp = client.get(f"/files/metadata?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == "grib"
        assert body["format"] == "grib2"
        assert body["metadata"] == expected
        assert "t2m" in body["metadata"]

    def test_multi_hypercube_concatenates_each_dataset(self, tmp_path):
        f = tmp_path / "multi.grib2"
        _write_grib(f, [("2t", "heightAboveGround", 2), ("t", "isobaricInhPa", 500)])

        resp = client.get(f"/files/metadata?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == "grib"
        assert "--- hypercube 1/2 ---" in body["metadata"]
        assert "--- hypercube 2/2 ---" in body["metadata"]
        assert "t2m" in body["metadata"]
        assert re.search(r"\bt\b", body["metadata"])


class TestLocalOtherFormats:
    def test_hdf4_returns_null_metadata_placeholder(self, tmp_path):
        f = tmp_path / "test.hdf"
        f.write_bytes(HDF4 + b"\x00" * 100)

        resp = client.get(f"/files/metadata?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == "hdf"
        assert body["metadata"] is None

    def test_zarr_returns_null_metadata_placeholder(self, tmp_path):
        store = tmp_path / "data.zarr"
        store.mkdir()
        (store / "zarr.json").write_text("{}")

        resp = client.get(f"/files/metadata?path={store}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] == "zarr"
        assert body["metadata"] is None

    def test_unrecognized_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(NOT_RECOGNIZED + b"\x00" * 100)

        resp = client.get(f"/files/metadata?path={f}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["family"] is None
        assert body["metadata"] is None

    def test_missing_file_returns_404(self):
        resp = client.get("/files/metadata?path=/nonexistent/file.dat")

        assert resp.status_code == 404
        assert "File not found" in resp.json()["detail"]


class TestURL:
    def test_url_returns_501(self):
        resp = client.get("/files/metadata?path=https://example.com/test.nc")

        assert resp.status_code == 501
        assert "not yet supported" in resp.json()["detail"]
