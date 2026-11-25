"""Tests for POST /files/check-compliance: the NetCDF checks, the scan step, and the endpoint.

Tasks run eagerly (see conftest.py), so no Redis or worker is needed. The Boreas
S3 client is mocked, and /glade/ is pointed at pytest's tmp dir.
"""
import json
import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr
from celery.result import AsyncResult
from fastapi.testclient import TestClient

from app.main import app
from app.tasks.portal import compliance
from app.tasks.portal.compliance_checks import base, check_file, netcdf
from app.tasks.portal.compliance_checks.zarr import check as zarr_check
from app.utils import callback

client = TestClient(app)

CALLBACK = "https://portal/cb"


@pytest.fixture(autouse=True)
def glade_root(tmp_path, monkeypatch):
    """Treat pytest's tmp dir as /glade/ (realpath, since macOS tmp dirs are symlinks)."""
    root = os.path.realpath(tmp_path) + os.sep
    monkeypatch.setattr("app.utils.file_validation.GLADE_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def boreas():
    """Stand in for the Boreas S3 client; yields put_object to inspect the uploaded log."""
    s3 = MagicMock()
    with patch("app.utils.job_log.s3_client", return_value=s3):
        yield s3.put_object


def build_dataset(*, time_attrs=None, lat_attrs=None, var_attrs=None, global_attrs=None, drop_time=False):
    """A small xarray Dataset; keyword args override pieces of the compliant default."""
    time_attrs = {"units": "days since 2000-01-01", "calendar": "standard", "standard_name": "time"} \
        if time_attrs is None else time_attrs
    lat_attrs = {"units": "degrees_north", "standard_name": "latitude"} if lat_attrs is None else lat_attrs
    var_attrs = {"long_name": "2m temperature", "units": "K"} if var_attrs is None else var_attrs
    global_attrs = {"title": "t", "summary": "s", "institution": "i", "source": "src",
                    "history": "h", "Conventions": "CF-1.8"} if global_attrs is None else global_attrs
    coords = {"lat": ("lat", np.array([10.0, 20.0]), lat_attrs),
              "lon": ("lon", np.array([100.0, 110.0, 120.0]),
                      {"units": "degrees_east", "standard_name": "longitude"})}
    dims = ["lat", "lon"]
    shape = (2, 3)
    if not drop_time:
        coords["time"] = ("time", np.array([0.0, 1.0]), time_attrs)
        dims.insert(0, "time")
        shape = (2, 2, 3)
    return xr.Dataset({"t2m": (dims, np.zeros(shape), var_attrs)}, coords=coords, attrs=global_attrs)


def make_nc(directory, name="good.nc", *, fill=-999.0, **overrides):
    """Write a small NetCDF file (see build_dataset for the overrides). fill=None writes no _FillValue."""
    path = os.path.join(directory, name)
    build_dataset(**overrides).to_netcdf(path, encoding={"t2m": {"_FillValue": fill}})
    return path


def make_zarr(directory, name="good.zarr", *, fill=-999.0, zarr_format=2, **overrides):
    """Write a small Zarr store (see build_dataset for the overrides). fill=None writes no _FillValue."""
    path = os.path.join(directory, name)
    build_dataset(**overrides).to_zarr(path, zarr_format=zarr_format, mode="w",
                                       encoding={"t2m": {"_FillValue": fill}})
    return path


def ids(findings):
    """The check ids in a list of findings."""
    return sorted(f["check"] for f in findings)


class TestNetcdfChecks:
    def test_compliant_file_has_no_errors_or_warnings(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path), "netcdf4")
        assert report["errors"] == [] and report["warnings"] == []
        assert report["status"] == "checked"

    def test_missing_time_variable(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, drop_time=True))
        assert ids(report["errors"]) == [base.TIME_MISSING]

    def test_time_without_units_or_calendar(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, time_attrs={"standard_name": "time"}))
        assert ids(report["errors"]) == [base.TIME_CALENDAR_MISSING, base.TIME_UNITS_MISSING]

    def test_time_without_standard_name_is_only_a_warning(self, tmp_path):
        attrs = {"units": "days since 2000-01-01", "calendar": "standard"}
        report = netcdf.check(make_nc(tmp_path, time_attrs=attrs))
        assert report["errors"] == []
        assert ids(report["warnings"]) == [base.TIME_STANDARD_NAME]

    def test_lat_without_units_is_an_error(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, lat_attrs={"standard_name": "latitude"}))
        assert ids(report["errors"]) == [base.LATLON_UNITS_MISSING]

    def test_lat_without_standard_name_is_a_warning(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, lat_attrs={"units": "degrees_north"}))
        assert report["errors"] == []
        assert ids(report["warnings"]) == [base.LATLON_STANDARD_NAME]

    def test_data_var_missing_long_name_and_units(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, var_attrs={"foo": "bar"}))
        assert ids(report["errors"]) == [base.VAR_LONG_NAME_MISSING, base.VAR_UNITS_MISSING]

    def test_blank_units_is_only_a_warning(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, var_attrs={"long_name": "x", "units": " "}))
        assert report["errors"] == []
        assert ids(report["warnings"]) == [base.VAR_ATTR_EMPTY]

    def test_dimensionless_units_one_is_accepted(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, var_attrs={"long_name": "fraction", "units": "1"}))
        assert report["errors"] == []

    def test_flag_variable_does_not_need_units(self, tmp_path):
        attrs = {"long_name": "quality", "flag_values": np.array([0, 1]), "flag_meanings": "good bad"}
        report = netcdf.check(make_nc(tmp_path, var_attrs=attrs))
        assert report["errors"] == []

    def test_missing_fill_value_is_only_a_warning(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, fill=None))
        assert report["errors"] == []
        assert ids(report["warnings"]) == [base.VAR_FILL_VALUE_MISSING]

    def test_missing_value_attribute_also_counts(self, tmp_path):
        attrs = {"long_name": "x", "units": "K", "missing_value": -1.0}
        report = netcdf.check(make_nc(tmp_path, var_attrs=attrs, fill=None))
        assert report["errors"] == []

    def test_missing_global_attributes_are_warnings(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, global_attrs={"description": "d", "conventions": "CF"}))
        assert report["errors"] == []
        messages = " ".join(w["message"] for w in report["warnings"])
        assert ids(report["warnings"]) == [base.GLOBAL_ATTR_MISSING] * 4
        for name in ("title", "institution", "source", "history"):
            assert name in messages
        assert "summary" not in messages and "onventions" not in messages  # description / lowercase conventions count

    def test_coordinates_never_need_a_fill_value(self, tmp_path):
        """build_dataset's time/lat/lon are written with no _FillValue, and only the data variable is held to it."""
        ds = build_dataset()
        path = str(tmp_path / "coords.nc")
        ds.to_netcdf(path, encoding={"t2m": {"_FillValue": -999.0}, "time": {"_FillValue": None},
                                     "lat": {"_FillValue": None}, "lon": {"_FillValue": None}})
        assert netcdf.check(path)["errors"] == []

    def test_unlinked_latlon_are_treated_as_coordinates(self, tmp_path):
        """2-D lat/lon with no `coordinates` attribute land in data_vars; they still need no fill value."""
        lat = {"units": "degrees_north", "standard_name": "latitude"}
        lon = {"units": "degrees_east", "standard_name": "longitude"}
        ds = xr.Dataset(
            {"t2m": (["time", "y", "x"], np.zeros((2, 2, 2)), {"long_name": "t", "units": "K"}),
             "lat": (("y", "x"), np.zeros((2, 2)), lat), "lon": (("y", "x"), np.zeros((2, 2)), lon)},
            coords={"time": ("time", [0.0, 1.0], {"units": "days since 2000-01-01", "calendar": "standard",
                                                  "standard_name": "time"})},
            attrs={"title": "t", "summary": "s", "institution": "i", "source": "s", "history": "h",
                   "Conventions": "CF-1.8"})
        path = str(tmp_path / "curvilinear.nc")
        ds.to_netcdf(path, encoding={"t2m": {"_FillValue": -9.0}, "lat": {"_FillValue": None},
                                     "lon": {"_FillValue": None}, "time": {"_FillValue": None}})
        assert netcdf.check(path)["errors"] == []

    def test_unlinked_latlon_still_need_units(self, tmp_path):
        """Being exempt from the data-variable rules doesn't exempt lat/lon from their own rules."""
        ds = xr.Dataset(
            {"t2m": (["time", "y", "x"], np.zeros((1, 2, 2)), {"long_name": "t", "units": "K"}),
             "lat": (("y", "x"), np.zeros((2, 2)), {"standard_name": "latitude"})},
            coords={"time": ("time", [0.0], {"units": "days since 2000-01-01", "calendar": "standard"})})
        path = str(tmp_path / "nounits.nc")
        ds.to_netcdf(path, encoding={"t2m": {"_FillValue": -9.0}, "lat": {"_FillValue": None}})
        assert ids(netcdf.check(path)["errors"]) == [base.LATLON_UNITS_MISSING]

    def test_ordinary_data_variable_is_still_warned_about_a_missing_fill_value(self, tmp_path):
        """The coordinate exemption is narrow: only the data variable is warned, never time/lat/lon."""
        report = netcdf.check(make_nc(tmp_path, fill=None))
        assert [w["variable"] for w in report["warnings"]] == ["t2m"]

    def test_unreadable_file_is_an_error_not_a_crash(self, tmp_path):
        bad = tmp_path / "bad.nc"
        bad.write_bytes(b"CDF\x01 this is not really netcdf")
        report = netcdf.check(str(bad))
        assert ids(report["errors"]) == [base.UNREADABLE]


@pytest.mark.parametrize("zarr_format", [2, 3])
class TestZarrChecks:
    """Zarr runs the same checks as NetCDF, on stores of either Zarr version."""

    def test_compliant_store_has_no_errors_or_warnings(self, tmp_path, zarr_format):
        report = zarr_check(make_zarr(tmp_path, zarr_format=zarr_format))
        assert report["errors"] == [] and report["warnings"] == []
        assert report["status"] == "checked" and report["format"] == "zarr"

    def test_missing_time_variable(self, tmp_path, zarr_format):
        report = zarr_check(make_zarr(tmp_path, zarr_format=zarr_format, drop_time=True))
        assert ids(report["errors"]) == [base.TIME_MISSING]

    def test_time_without_units_or_calendar(self, tmp_path, zarr_format):
        report = zarr_check(make_zarr(tmp_path, zarr_format=zarr_format, time_attrs={"standard_name": "time"}))
        assert ids(report["errors"]) == [base.TIME_CALENDAR_MISSING, base.TIME_UNITS_MISSING]

    def test_data_var_missing_long_name_and_units(self, tmp_path, zarr_format):
        report = zarr_check(make_zarr(tmp_path, zarr_format=zarr_format, var_attrs={"foo": "bar"}))
        assert ids(report["errors"]) == [base.VAR_LONG_NAME_MISSING, base.VAR_UNITS_MISSING]

    def test_missing_fill_value(self, tmp_path, zarr_format):
        """No _FillValue is still noticed (as a warning) even though Zarr v3 always has a default fill_value in its metadata."""
        report = zarr_check(make_zarr(tmp_path, zarr_format=zarr_format, fill=None))
        assert report["errors"] == []
        assert ids(report["warnings"]) == [base.VAR_FILL_VALUE_MISSING]

    def test_missing_global_attributes_are_warnings(self, tmp_path, zarr_format):
        report = zarr_check(make_zarr(tmp_path, zarr_format=zarr_format, global_attrs={"title": "t"}))
        assert report["errors"] == []
        # everything but title: summary/description, institution, source, history, Conventions
        assert ids(report["warnings"]) == [base.GLOBAL_ATTR_MISSING] * 5

    def test_stale_consolidated_metadata_does_not_hide_a_problem(self, tmp_path, zarr_format):
        path = make_zarr(tmp_path, zarr_format=zarr_format)
        if zarr_format == 2:
            xr.open_zarr(path, consolidated=False).to_zarr(path, mode="w", consolidated=True, zarr_format=2)
        assert zarr_check(path)["errors"] == []

    def test_broken_store_is_an_error_not_a_crash(self, tmp_path, zarr_format):
        store = tmp_path / "broken.zarr"
        store.mkdir()
        (store / ("zarr.json" if zarr_format == 3 else ".zgroup")).write_text("not json")
        assert ids(zarr_check(str(store))["errors"]) == [base.UNREADABLE]


class TestFindingShape:
    def test_findings_have_no_severity_key(self, tmp_path):
        """Severity is implied by errors vs warnings, so it isn't repeated on each finding."""
        report = netcdf.check(make_nc(tmp_path, drop_time=True, fill=None, global_attrs={"title": "t"}))
        assert report["errors"] and report["warnings"]
        for f in report["errors"] + report["warnings"]:
            assert set(f) == {"check", "variable", "message"}

    def test_errors_and_warnings_are_still_sorted_into_the_right_lists(self, tmp_path):
        report = netcdf.check(make_nc(tmp_path, drop_time=True, fill=None))
        assert ids(report["errors"]) == [base.TIME_MISSING]
        assert base.VAR_FILL_VALUE_MISSING in ids(report["warnings"])


class TestDispatch:
    def test_unknown_format_is_skipped(self, tmp_path):
        p = tmp_path / "notes.txt"
        p.write_text("hello")
        report = check_file(str(p))
        assert report["status"] == "skipped" and report["errors"] == []

    def test_grib_is_an_unsupported_placeholder(self, tmp_path):
        p = tmp_path / "x.grib2"
        p.write_bytes(b"GRIB\x00\x00\x00\x02" + b"\x00" * 16)
        report = check_file(str(p))
        assert report["status"] == "unsupported" and report["errors"] == []
        assert report["format"] == "grib2"

    def test_netcdf_is_dispatched(self, tmp_path):
        assert check_file(make_nc(tmp_path))["format"] == "netcdf4"


def scan(path, recursive=True):
    """Run the scan_files step on a fresh report; returns (result, details, log lines)."""
    report = compliance.new_report(path, compliance.COMPLIANCE_STEPS, "check-compliance")
    report["recursive"] = recursive
    result, details = compliance._scan_files(path, report)
    return result, details, report["_log"]["json_lines"]


class TestScanFiles:
    def test_all_files_are_scanned_and_one_failure_fails_the_scan(self, tmp_path):
        make_nc(tmp_path, "a_good.nc")
        make_nc(tmp_path, "b_bad.nc", drop_time=True)
        make_nc(tmp_path, "c_good.nc")
        (tmp_path / "notes.txt").write_text("hi")
        (tmp_path / "x.grib2").write_bytes(b"GRIB\x00\x00\x00\x02" + b"\x00" * 16)
        result, d, log = scan(str(tmp_path))
        assert result is False and d["compliant"] is False
        assert d["files_scanned"] == 5 and d["files_checked"] == 3
        assert d["files_failed"] == 1 and d["files_unsupported"] == 1 and d["files_skipped"] == 1
        assert [f["path"] for f in d["failed_files"]] == [str(tmp_path / "b_bad.nc")]
        # one log line per file, written for every file, passing or not
        file_lines = [line for line in log if "path" in line and line["command"] == "scan_files"]
        assert len(file_lines) == 5

    def test_everything_passing_is_compliant(self, tmp_path):
        make_nc(tmp_path, "a.nc")
        result, d, _ = scan(str(tmp_path))
        assert result is True and d["compliant"] is True and d["failed_files"] == []

    def test_warning_only_files_are_listed_but_pass(self, tmp_path):
        make_nc(tmp_path, "a.nc", global_attrs={"title": "only a title"})
        result, d, _ = scan(str(tmp_path))
        assert result is True
        assert d["files_with_warnings"] == 1 and len(d["warning_files"]) == 1

    def test_listed_files_are_capped_at_20_but_counts_are_exact(self, tmp_path):
        for i in range(25):
            make_nc(tmp_path, f"bad_{i:02d}.nc", drop_time=True)
        result, d, log = scan(str(tmp_path))
        assert d["files_failed"] == 25
        assert len(d["failed_files"]) == 20 and d["failed_files_truncated"] is True
        assert len([line for line in log if line["command"] == "scan_files" and "path" in line]) == 25

    def test_empty_directory_is_not_compliant(self, tmp_path):
        result, d, _ = scan(str(tmp_path))
        assert result is False and d["files_scanned"] == 0

    def test_non_recursive_ignores_subdirectories(self, tmp_path):
        make_nc(tmp_path, "top.nc")
        sub = tmp_path / "sub"
        sub.mkdir()
        make_nc(sub, "deep.nc", drop_time=True)
        assert scan(str(tmp_path), recursive=False)[1]["files_scanned"] == 1
        assert scan(str(tmp_path), recursive=True)[1]["files_scanned"] == 2

    def test_single_file_path(self, tmp_path):
        assert scan(make_nc(tmp_path))[0] is True

    def test_symlink_pointing_outside_root_is_skipped_not_opened(self, tmp_path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside")
        target = make_nc(outside, "secret.nc")
        root = tmp_path / "data"
        root.mkdir()
        os.symlink(target, root / "link.nc")
        with patch("app.utils.file_validation.GLADE_ROOT", os.path.realpath(root) + os.sep):
            result, d, _ = scan(str(root))
        assert d["files_skipped"] == 1 and d["files_checked"] == 0

    def test_zarr_store_is_one_checked_target_not_its_chunk_files(self, tmp_path):
        make_zarr(tmp_path, "good.zarr")
        result, d, _ = scan(str(tmp_path))
        assert result is True
        assert d["files_scanned"] == 1 and d["files_checked"] == 1

    def test_bad_zarr_store_fails_the_scan_alongside_good_netcdf(self, tmp_path):
        make_nc(tmp_path, "a.nc")
        make_zarr(tmp_path, "b.zarr", drop_time=True)
        result, d, _ = scan(str(tmp_path))
        assert result is False and d["files_scanned"] == 2 and d["files_failed"] == 1
        assert d["failed_files"][0]["path"] == str(tmp_path / "b.zarr")

    def test_zarr_store_as_the_scanned_path(self, tmp_path):
        assert scan(make_zarr(tmp_path))[0] is True


class TestEndpoint:
    def post(self, **body):
        return client.post("/files/check-compliance", json=body)

    def test_path_outside_glade_is_403(self):
        assert self.post(Path="/etc/passwd").status_code == 403

    def test_dotdot_escape_is_403(self, glade_root):
        assert self.post(Path=glade_root + "../../etc").status_code == 403

    def test_relative_path_is_403(self):
        assert self.post(Path="data/file.nc").status_code == 403

    def test_missing_path_is_404(self, glade_root):
        assert self.post(Path=glade_root + "nope").status_code == 404

    def test_callback_not_allowlisted_is_403(self, tmp_path):
        r = self.post(Path=str(tmp_path), CallbackUrl="https://evil.example/hook")
        assert r.status_code == 403

    def test_submitted_job_runs_and_reports(self, tmp_path, boreas):
        make_nc(tmp_path, "a.nc")
        make_nc(tmp_path, "b.nc", drop_time=True)
        with patch("app.utils.callback.httpx.post"):
            r = self.post(Path=str(tmp_path), CallbackUrl=CALLBACK)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "SUBMITTED" and body["result"] is None

        result = AsyncResult(body["external_job_id"]).result
        scan_step = result["tasks"]["scan_files"]
        assert scan_step["status"] == "SUCCESS" and scan_step["result"] is False
        assert scan_step["details"]["files_failed"] == 1
        assert result["log_url"].endswith(f"check_compliance.{body['external_job_id']}.jsonl")
        # the uploaded log has a line for each file
        lines = [json.loads(x) for x in boreas.call_args.kwargs["Body"].decode().splitlines()]
        assert len([x for x in lines if x["command"] == "scan_files" and "path" in x]) == 2
