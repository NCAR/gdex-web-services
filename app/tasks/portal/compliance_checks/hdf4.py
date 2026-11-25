"""GDEX compliance checks for HDF4 files. PLACEHOLDER: not implemented yet."""
from app.tasks.portal.compliance_checks.base import unsupported


def check(path, fmt=None):
  """Return an "unsupported" file report.

  TODO: define GDEX compliance for HDF4 and implement it here (needs an HDF4
  reader such as pyhdf, which is not installed).
  """
  return unsupported(path, fmt, "HDF4")
