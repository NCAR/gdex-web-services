"""Shared types for the per-format compliance checkers.

Every format module in this package exposes check(path) -> FileReport, built
with the helpers here, so scan_files can treat all formats the same way.

A finding is one thing wrong (severity "error") or worth a look (severity
"warning") in a file:

    {"check": "<CHECK_ID>", "severity": "error" | "warning",
     "variable": "<name>" | null, "message": "<human readable>"}

A file report is:

    {"path": "<path>", "format": "<format or null>",
     "status": "checked" | "unsupported" | "skipped",
     "errors": [<finding>, ...], "warnings": [<finding>, ...]}

Only errors make a file non-compliant. "unsupported" (a format we recognise
but have no checker for yet) and "skipped" (not a recognised format) never fail
a scan.
"""

# Check ids. They are constants so the portal can key on them.
TIME_MISSING = "time_missing"
TIME_UNITS_MISSING = "time_units_missing"
TIME_CALENDAR_MISSING = "time_calendar_missing"
TIME_STANDARD_NAME = "time_standard_name"
NO_COORDINATES = "no_coordinates"
VAR_NO_COORDINATES = "variable_no_coordinates"
LATLON_UNITS_MISSING = "latlon_units_missing"
LATLON_STANDARD_NAME = "latlon_standard_name"
VAR_LONG_NAME_MISSING = "variable_long_name_missing"
VAR_UNITS_MISSING = "variable_units_missing"
VAR_ATTR_EMPTY = "variable_attribute_empty"
VAR_FILL_VALUE_MISSING = "variable_fill_value_missing"
GLOBAL_ATTR_MISSING = "global_attribute_missing"
UNREADABLE = "unreadable"
UNSUPPORTED_FORMAT = "unsupported_format"
UNKNOWN_FORMAT = "unknown_format"
OUTSIDE_ROOT = "outside_allowed_root"

ERROR = "error"
WARNING = "warning"


def finding(check, severity, message, variable=None):
  """Build one finding dict (see the module docstring for the shape)."""
  return {"check": check, "severity": severity, "variable": variable, "message": message}


def file_report(path, fmt, status, findings=()):
  """Build a file report, sorting the findings into its errors and warnings lists."""
  return {
      "path": path,
      "format": fmt,
      "status": status,
      "errors": [f for f in findings if f["severity"] == ERROR],
      "warnings": [f for f in findings if f["severity"] == WARNING],
  }


def unsupported(path, fmt, family):
  """File report for a recognised format that has no compliance checker yet.

  Used by the placeholder modules (grib, bufr, ...). It carries one warning so
  the gap shows up in the log, but it never fails the scan.
  """
  return file_report(path, fmt, "unsupported", [
      finding(UNSUPPORTED_FORMAT, WARNING, f"No GDEX compliance checks exist yet for {family} files; not checked"),
  ])
