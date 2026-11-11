"""GDEX compliance checks for BUFR files. PLACEHOLDER: not implemented yet."""
from app.tasks.portal.compliance_checks.base import unsupported


def check(path, fmt=None):
  """Return an "unsupported" file report.

  TODO: define GDEX compliance for BUFR (point observations) and implement it here.
  """
  return unsupported(path, fmt, "BUFR")
