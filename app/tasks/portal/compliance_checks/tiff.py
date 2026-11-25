"""GDEX compliance checks for TIFF/GeoTIFF files. PLACEHOLDER: not implemented yet."""
from app.tasks.portal.compliance_checks.base import unsupported


def check(path, fmt=None):
  """Return an "unsupported" file report.

  TODO: define GDEX compliance for GeoTIFF and implement it here.
  """
  return unsupported(path, fmt, "TIFF")
