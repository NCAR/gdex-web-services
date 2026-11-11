"""GDEX compliance checks for NetCDF (classic and netCDF-4/HDF5) files.

Opens the file with xarray and runs the shared checks in cf_checks.py (see
there for exactly what is an error and what is a warning).
"""
import xarray as xr

from app.tasks.portal.compliance_checks import base, cf_checks
from app.tasks.portal.compliance_checks.base import ERROR, finding


def _open(path):
  """Open a NetCDF file lazily with the raw attributes intact.

  decode_times=False and mask_and_scale=False keep "units", "calendar" and
  "_FillValue" in .attrs, where xarray's default decoding would move them into
  .encoding. decode_coords="all" still sorts auxiliary coordinates, bounds and
  grid-mapping variables out of data_vars.
  """
  return xr.open_dataset(path, decode_times=False, decode_timedelta=False,
                         mask_and_scale=False, decode_coords="all")


def check(path, fmt=None):
  """Run every check on one NetCDF file and return its file report.

  A file xarray can't open (corrupt, plain HDF5 that isn't netCDF-4, ...) gets one
  "unreadable" error rather than raising, so the rest of the scan carries on.
  """
  try:
    ds = _open(path)
  except Exception as e:
    return base.file_report(path, fmt, "checked", [
        finding(base.UNREADABLE, ERROR, f"Could not open as NetCDF: {e}"),
    ])
  try:
    findings = cf_checks.check_dataset(ds)
  finally:
    ds.close()
  return base.file_report(path, fmt, "checked", findings)
