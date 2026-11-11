"""GDEX compliance checks for GRIB (GRIB1/GRIB2) files. PLACEHOLDER: not implemented yet."""
from app.tasks.portal.compliance_checks.base import unsupported


def check(path, fmt=None):
  """Return an "unsupported" file report.

  TODO: GRIB has no CF attributes to check; decide what "GDEX compliant" means
  for it (e.g. via cfgrib, which is already installed) and implement it here.
  """
  return unsupported(path, fmt, "GRIB")
