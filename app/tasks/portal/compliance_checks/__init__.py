"""Per-format GDEX compliance checkers, and the dispatcher that picks one for a file.

One module per file format; each exposes check(path, fmt=None) -> file report
(see base.py). To add a format: write its module here and add its family to
CHECKERS. Families are the names app.utils.file_formats._detect_file_format returns.

Implemented: netcdf (netCDF-3 and netCDF-4/HDF5) and zarr, which share the checks in cf_checks.py.
Placeholders (report "unsupported"): grib, bufr, hdf (HDF4), tiff.
Zarr is a directory with no magic bytes, so scan_files routes it to zarr.check directly
instead of going through check_file.
"""
from app.tasks.portal.compliance_checks import base, bufr, grib, hdf4, netcdf, tiff, zarr
from app.utils.file_formats import _detect_file_format, read_local_header

# format family -> checker
CHECKERS = {
    "netcdf": netcdf.check,
    "grib": grib.check,
    "bufr": bufr.check,
    "hdf": hdf4.check,
    "tiff": tiff.check,
}


def check_file(path):
  """Detect a file's format from its first bytes and run the matching checker.

  Returns a file report. A file in no recognised format is "skipped" with a
  warning; one that can't be read at all gets an "unreadable" error.
  """
  try:
    family, fmt = _detect_file_format(read_local_header(path))
  except OSError as e:
    return base.file_report(path, None, "checked", [
        base.finding(base.UNREADABLE, base.ERROR, f"Could not read file: {e}"),
    ])
  checker = CHECKERS.get(family)
  if checker is None:
    return base.file_report(path, None, "skipped", [
        base.finding(base.UNKNOWN_FORMAT, base.WARNING, "Not a recognised data format; skipped"),
    ])
  return checker(path, fmt)
