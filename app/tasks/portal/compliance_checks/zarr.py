"""GDEX compliance checks for Zarr stores (v2 and v3).

A Zarr store is a directory, not a file, so it has no magic bytes: scan_files
recognises one by its metadata file (see ZARR_MARKERS) and hands the directory
to check() instead of descending into it. The checks are the same as for
NetCDF (cf_checks.py); only how the dataset is opened differs.
"""
import os

import xarray as xr

from app.tasks.portal.compliance_checks import base, cf_checks
from app.tasks.portal.compliance_checks.base import ERROR, finding

# Files whose presence marks a directory as a Zarr store (v2 and v3).
ZARR_MARKERS = (".zgroup", ".zarray", "zarr.json")


def is_zarr_store(directory):
  """True if the directory holds a Zarr metadata file."""
  return any(os.path.exists(os.path.join(directory, m)) for m in ZARR_MARKERS)


def _open(path):
  """Open a Zarr store lazily with the raw attributes intact (same options as netcdf._open).

  consolidated=False reads each array's own metadata, so a stale consolidated
  .zmetadata can't hide a missing attribute, and no fallback warning is raised.
  Only metadata is read; no chunks are loaded.
  """
  return xr.open_dataset(path, engine="zarr", consolidated=False, decode_times=False,
                         decode_timedelta=False, mask_and_scale=False, decode_coords="all")


def check(path, fmt=None):
  """Run every check on one Zarr store and return its file report.

  A store xarray can't open (corrupt, or the zarr package missing) gets one
  "unreadable" error rather than raising, so the rest of the scan carries on.
  """
  fmt = fmt or "zarr"
  try:
    ds = _open(path)
  except Exception as e:
    return base.file_report(path, fmt, "checked", [
        finding(base.UNREADABLE, ERROR, f"Could not open as Zarr: {e}"),
    ])
  try:
    findings = cf_checks.check_dataset(ds)
  finally:
    ds.close()
  return base.file_report(path, fmt, "checked", findings)
