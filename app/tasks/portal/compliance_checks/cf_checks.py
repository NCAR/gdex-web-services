"""The GDEX/CF compliance checks themselves, run on an already-open xarray Dataset.

Format-independent: netcdf.py and zarr.py each open their own kind of file and
hand the Dataset to check_dataset. The Dataset must be opened with the raw
attributes intact (decode_times=False, mask_and_scale=False,
decode_coords="all"), so "units", "calendar" and "_FillValue" are in .attrs
and ds.coords / ds.data_vars separate coordinates, bounds and grid-mapping
variables from data variables the way CF does.

Errors (any one makes the file non-compliant):
  - no time variable, or the time variable has no units or no calendar
  - the dataset has no coordinates, or a data variable has none
  - a latitude/longitude variable has no units
  - a data variable has no long_name, or no units (unless CF exempts it)

Warnings (reported, but the file still passes):
  - a data variable has no _FillValue or missing_value
    (coordinates, including time and lat/lon, never need one)
  - the time variable has no standard_name "time"
  - a latitude/longitude variable has no standard_name
  - a data variable's long_name or units is present but blank
  - a global attribute is missing: title, summary/description, institution,
    source, history, Conventions

Only the root group is checked.
"""
import re

from app.tasks.portal.compliance_checks import base
from app.tasks.portal.compliance_checks.base import ERROR, WARNING, finding

# Global attributes GDEX wants. Each entry is the names that satisfy it (any one will do).
GLOBAL_ATTRIBUTES = (
    ("title",),
    ("summary", "description"),
    ("institution",),
    ("source",),
    ("history",),
    ("conventions",),
)

_LAT_NAMES = {"lat", "latitude"}
_LON_NAMES = {"lon", "longitude"}
_LAT_UNITS = {"degrees_north", "degree_north", "degrees_n", "degree_n", "degreesn", "degreen"}
_LON_UNITS = {"degrees_east", "degree_east", "degrees_e", "degree_e", "degreese", "degreee"}
_SINCE = re.compile(r"\bsince\b", re.IGNORECASE)
_BOUNDS_NAME = re.compile(r"(_bnds|_bounds)$", re.IGNORECASE)
_FLAG_ATTRS = ("flag_values", "flag_masks", "flag_meanings")
_FILL_ATTRS = ("_FillValue", "missing_value")


def _text(value):
  """An attribute value as a stripped string ("" if it isn't one)."""
  return value.strip() if isinstance(value, str) else ""


def _is_time(name, var):
  """True if the variable looks like CF time: standard_name, axis T, "<unit> since <date>" units, or the name."""
  attrs = var.attrs
  return (_text(attrs.get("standard_name")).lower() == "time"
          or _text(attrs.get("axis")).upper() == "T"
          or bool(_SINCE.search(_text(attrs.get("units"))))
          or str(name).lower() == "time")


def _lat_lon_kind(name, var):
  """Return "lat", "lon", or None for a variable, by standard_name, units, then name."""
  attrs = var.attrs
  standard_name = _text(attrs.get("standard_name")).lower()
  units = _text(attrs.get("units")).lower()
  lowered = str(name).lower()
  if standard_name == "latitude" or units in _LAT_UNITS or lowered in _LAT_NAMES:
    return "lat"
  if standard_name == "longitude" or units in _LON_UNITS or lowered in _LON_NAMES:
    return "lon"
  return None


def _is_coordinate_like(name, var):
  """True if the variable is a coordinate even when xarray files it under data_vars.

  xarray only treats a variable as a coordinate if it is a dimension
  coordinate or is named in a `coordinates` attribute. Files that leave that
  attribute out (common for 2-D lat/lon on curvilinear grids) would otherwise
  have their coordinates held to the data-variable rules. Coordinates need no
  _FillValue/missing_value, long_name or coordinates of their own, so
  variables that identify themselves as time, latitude/longitude, or an
  X/Y/Z/T axis are exempt from those rules. Their own rules (time units and
  calendar, lat/lon units) still apply via check_time and check_lat_lon.
  """
  return (_is_time(name, var)
          or _lat_lon_kind(name, var) is not None
          or _text(var.attrs.get("axis")).upper() in {"X", "Y", "Z", "T"})


def check_time(ds):
  """Check the time variable: it exists, has units and a calendar, and (warning) standard_name "time".

  Returns a list of findings. A dataset with no time variable gets one error and
  nothing else, since there is nothing further to check.
  """
  candidates = [n for n in ds.variables if _is_time(n, ds[n])]
  if not candidates:
    return [finding(base.TIME_MISSING, ERROR, "No time variable found")]
  # prefer a real coordinate over a data variable that merely looks like time
  name = next((n for n in candidates if n in ds.coords), candidates[0])
  attrs = ds[name].attrs
  out = []
  if not _text(attrs.get("units")):
    out.append(finding(base.TIME_UNITS_MISSING, ERROR, f"Time variable '{name}' has no units", name))
  if not _text(attrs.get("calendar")):
    out.append(finding(base.TIME_CALENDAR_MISSING, ERROR, f"Time variable '{name}' has no calendar", name))
  if _text(attrs.get("standard_name")).lower() != "time":
    out.append(finding(base.TIME_STANDARD_NAME, WARNING,
                       f"Time variable '{name}' should have standard_name 'time'", name))
  return out


def check_coordinates(ds):
  """Check the dataset has coordinates, and that every non-scalar data variable has some.

  Returns a list of findings.
  """
  if len(ds.coords) == 0:
    return [finding(base.NO_COORDINATES, ERROR, "Dataset has no coordinate variables")]
  return [
      finding(base.VAR_NO_COORDINATES, ERROR, f"Data variable '{name}' has no coordinates", str(name))
      for name in ds.data_vars
      if ds[name].ndim > 0 and len(ds[name].coords) == 0 and not _is_coordinate_like(name, ds[name])
  ]


def check_lat_lon(ds):
  """Check latitude/longitude variables, if any, have units (error) and a standard_name (warning).

  A dataset with no lat/lon variable (e.g. a station time series) is not an error.
  Returns a list of findings.
  """
  out = []
  for name in ds.variables:
    kind = _lat_lon_kind(name, ds[name])
    if kind is None:
      continue
    label = "Latitude" if kind == "lat" else "Longitude"
    attrs = ds[name].attrs
    if not _text(attrs.get("units")):
      out.append(finding(base.LATLON_UNITS_MISSING, ERROR, f"{label} variable '{name}' has no units", str(name)))
    if not _text(attrs.get("standard_name")):
      out.append(finding(base.LATLON_STANDARD_NAME, WARNING,
                         f"{label} variable '{name}' has no standard_name", str(name)))
  return out


def _units_exempt(var):
  """True if CF doesn't require units on this variable: string/char data, or a flag variable."""
  return var.dtype.kind in "SUO" or any(a in var.attrs for a in _FLAG_ATTRS)


def _has_fill_value(var):
  """True if the variable declares _FillValue or missing_value.

  Looked for in .attrs, and in .encoding only when it holds a real value (the
  Zarr backend always creates an `_FillValue: None` encoding key, and its
  `fill_value` is a storage default, so neither counts). Note a Zarr v2 array
  created with zarr-python's default fill value of 0 is reported by xarray as
  _FillValue=0, and can't be told apart from a deliberate choice.
  """
  return any(k in var.attrs or var.encoding.get(k) is not None for k in _FILL_ATTRS)


def check_data_vars(ds):
  """Check each data variable has long_name, units (unless exempt) and a fill value.

  A missing long_name or units is an error. A missing _FillValue/missing_value,
  or a present-but-blank long_name/units, is a warning.
  Coordinates need none of these, so they are skipped: bounds variables, and
  anything _is_coordinate_like even if xarray lists it under data_vars.
  Returns a list of findings.
  """
  out = []
  for name in ds.data_vars:
    if _BOUNDS_NAME.search(str(name)) or _is_coordinate_like(name, ds[name]):
      continue
    var = ds[name]
    label = str(name)
    for attr, check_id, exempt in (("long_name", base.VAR_LONG_NAME_MISSING, False),
                                   ("units", base.VAR_UNITS_MISSING, _units_exempt(var))):
      if attr in var.attrs:
        if not _text(var.attrs[attr]) and not exempt:
          out.append(finding(base.VAR_ATTR_EMPTY, WARNING, f"Data variable '{name}' has an empty {attr}", label))
      elif not exempt:
        out.append(finding(check_id, ERROR, f"Data variable '{name}' has no {attr}", label))
    if not _has_fill_value(var) and var.dtype.kind not in "SUO":
      out.append(finding(base.VAR_FILL_VALUE_MISSING, WARNING,
                         f"Data variable '{name}' has no _FillValue or missing_value", label))
  return out


def check_globals(ds):
  """Warn about each missing global attribute (names are matched case-insensitively).

  Returns a list of findings.
  """
  present = {str(k).lower() for k, v in ds.attrs.items() if v not in (None, "")}
  return [
      finding(base.GLOBAL_ATTR_MISSING, WARNING, f"Missing global attribute: {' or '.join(names)}")
      for names in GLOBAL_ATTRIBUTES
      if not any(n in present for n in names)
  ]


def check_dataset(ds):
  """Run every check on an open Dataset and return the combined list of findings."""
  return (check_time(ds) + check_coordinates(ds) + check_lat_lon(ds)
          + check_data_vars(ds) + check_globals(ds))
