import os
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from app.main import app
from app.routers import generators

client = TestClient(app)


def _mock_s3_client():
    """Build a MagicMock standing in for the boto3 S3 client, recording put_object calls."""
    mock_client = MagicMock()
    mock_client.put_object = MagicMock(return_value={})
    return mock_client


@pytest.fixture(autouse=True)
def allow_tmp_path_as_root(tmp_path):
    """Treat tmp_path as the allowed root for these tests, since real files can't live under /glade/ here."""
    with patch("app.routers.generators._ALLOWED_ROOT", str(tmp_path) + os.sep):
        yield


@pytest.fixture
def netcdf_file(tmp_path):
    ds = xr.Dataset(
        {
            "temperature": (("x", "y"), np.random.rand(4, 5)),
            "pressure": (("x", "y"), np.random.rand(4, 5)),
        }
    )
    path = tmp_path / "test.nc"
    ds.to_netcdf(path)
    return path


class TestVisualize:
    def test_default_variable_is_first_data_var(self, netcdf_file):
        mock_client = _mock_s3_client()
        with patch("app.routers.generators._s3_client", return_value=mock_client):
            resp = client.post(f"/generators/visualize?path={netcdf_file}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["variable"] == "temperature"
        assert body["bucket"] == "gdex-data"
        assert body["key"].startswith("services_tmp/")
        assert body["key"].endswith(".png")
        assert body["location"] == f"https://boreas.ucar.edu/gdex-data/{body['key']}"

        mock_client.put_object.assert_called_once()
        _, kwargs = mock_client.put_object.call_args
        assert kwargs["Bucket"] == "gdex-data"
        assert kwargs["Key"] == body["key"]
        assert kwargs["ContentType"] == "image/png"
        assert kwargs["Body"].startswith(b"\x89PNG")

    def test_explicit_variable(self, netcdf_file):
        mock_client = _mock_s3_client()
        with patch("app.routers.generators._s3_client", return_value=mock_client):
            resp = client.post(f"/generators/visualize?path={netcdf_file}&variable=pressure")

        assert resp.status_code == 200
        assert resp.json()["variable"] == "pressure"

    def test_unknown_variable_returns_400(self, netcdf_file):
        with patch("app.routers.generators._s3_client", return_value=_mock_s3_client()):
            resp = client.post(f"/generators/visualize?path={netcdf_file}&variable=nope")

        assert resp.status_code == 400
        assert "nope" in resp.json()["detail"]

    def test_no_data_variables_returns_400(self, tmp_path):
        ds = xr.Dataset(coords={"x": [1, 2, 3]})
        path = tmp_path / "coords_only.nc"
        ds.to_netcdf(path)

        with patch("app.routers.generators._s3_client", return_value=_mock_s3_client()):
            resp = client.post(f"/generators/visualize?path={path}")

        assert resp.status_code == 400
        assert "no data variables" in resp.json()["detail"].lower()

    def test_missing_file_returns_404(self, tmp_path):
        resp = client.post(f"/generators/visualize?path={tmp_path}/nonexistent.nc")

        assert resp.status_code == 404
        assert "File not found" in resp.json()["detail"]

    def test_not_a_netcdf_file_returns_500(self, tmp_path):
        path = tmp_path / "not_netcdf.txt"
        path.write_text("hello world")

        with patch("app.routers.generators._s3_client", return_value=_mock_s3_client()):
            resp = client.post(f"/generators/visualize?path={path}")

        assert resp.status_code == 500


class TestFindTimeDim:
    def test_named_time(self):
        ds = xr.Dataset(coords={"time": pd.date_range("2020-01-01", periods=3)})
        assert generators._find_time_dim(ds) == "time"

    def test_calendar_attr_on_differently_named_dim(self, tmp_path):
        # In-memory datetime64 coords carry no CF encoding; only a real
        # to_netcdf/open_dataset round-trip populates units/calendar,
        # matching how the endpoint actually opens files.
        ds = xr.Dataset(coords={"valid_time": pd.date_range("2020-01-01", periods=3)})
        path = tmp_path / "calendar.nc"
        ds.to_netcdf(path)
        with xr.open_dataset(path) as reopened:
            assert generators._find_time_dim(reopened) == "valid_time"

    def test_units_since_on_undecoded_dim(self):
        ds = xr.Dataset(coords={"forecast_time": ("forecast_time", [0, 1, 2])})
        ds["forecast_time"].attrs["units"] = "hours since 2020-01-01"
        assert generators._find_time_dim(ds) == "forecast_time"

    def test_no_time_dim(self):
        ds = xr.Dataset(coords={"x": [1, 2, 3]})
        assert generators._find_time_dim(ds) is None


class TestSelectDataArray:
    def test_slices_named_time_dim_to_first_step(self):
        ds = xr.Dataset(
            {"temperature": (("time", "x"), np.random.rand(3, 4))},
            coords={"time": pd.date_range("2020-01-01", periods=3)},
        )
        var_name, da = generators._select_data_array(ds, None)
        assert var_name == "temperature"
        assert "time" not in da.dims
        assert da.shape == (4,)

    def test_slices_calendar_dim_with_different_name(self, tmp_path):
        ds = xr.Dataset(
            {"temperature": (("valid_time", "x"), np.random.rand(3, 4))},
            coords={"valid_time": pd.date_range("2020-01-01", periods=3)},
        )
        path = tmp_path / "calendar.nc"
        ds.to_netcdf(path)
        with xr.open_dataset(path) as reopened:
            _, da = generators._select_data_array(reopened, None)
            assert "valid_time" not in da.dims
            assert da.shape == (4,)

    def test_no_time_dim_leaves_variable_untouched(self):
        ds = xr.Dataset({"temperature": (("x", "y"), np.random.rand(4, 5))})
        _, da = generators._select_data_array(ds, None)
        assert set(da.dims) == {"x", "y"}
        assert da.shape == (4, 5)


class TestPathRestriction:
    def test_path_outside_allowed_root_returns_403(self, netcdf_file):
        # allow_tmp_path_as_root scopes the allowed root to tmp_path; anything
        # outside it (like the real default /glade/ restriction in prod) must
        # be rejected before the file is ever opened.
        with patch("app.routers.generators._s3_client", return_value=_mock_s3_client()):
            resp = client.post(f"/generators/visualize?path={netcdf_file.parent.parent}")

        assert resp.status_code == 403
        assert "must be under" in resp.json()["detail"]
