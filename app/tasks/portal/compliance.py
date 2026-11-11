"""POST /files/check-compliance: scan a /glade file or directory for GDEX compliance.

The steps run as a Celery chain, in order:
  validate_path   is it a path under /glade/ that exists?
  scan_files      check every file under it, against its format's compliance rules

Every file is scanned even after one fails, so the report lists everything
wrong, not just the first problem. One file with an error makes the whole scan
non-compliant (scan_files returns False, see below).

This file is laid out in two sections:

  1. Steps   the plain functions that do the work
  2. Tasks   the Celery tasks that run each step, and submit_compliance,
             which builds and queues the chain

The per-format rules live in app/tasks/portal/compliance_checks/.
"""
import os

from app.celery_app import celery_app
from app.tasks.portal.check_data import _validate_path as _validate_exists
from app.tasks.portal.common import _log, new_report, run_step, submit_portal_task
from app.tasks.portal.compliance_checks import base, check_file, zarr
from app.utils.file_validation import glade_path_validate
from celery import chain


#####################################################################
# 1. Steps
#
# Each step is step(path, report) -> (result, details). A step runs only
# if every earlier step returned True (see common.run_step).
#####################################################################

COMPLIANCE_STEPS = ("validate_path", "scan_files")

# How many failing / warning-only files are listed in the job's result. The
# counts are always exact, and every file's findings go to the Boreas log.
MAX_LISTED_FILES = 20


def _validate_path(path, report):
  """Step 1: check the path is under /glade/ (links resolved) and exists.

  The API already checked this, but a worker doesn't trust a queued job it
  didn't build, so it is checked again here.

  Success: returns (True, {"is_directory": true | false}).
  Failure: raises ValueError (outside /glade/, or a URL), or FileNotFoundError;
  run_step records it as FAILURE.
  """
  resolved = glade_path_validate(path) if not path.startswith(("http://", "https://")) else path
  # for a URL this raises the same "only supports paths on /glade" ValueError as check-data
  return _validate_exists(resolved, report)


def _iter_targets(path, recursive):
  """Yield (path, is_zarr) for each thing to check under `path`.

  A file yields itself. A directory yields its files (all levels if recursive,
  else only the top level), in sorted order, without following symlinked
  directories. A Zarr store is a directory: it is yielded once as
  (store_path, True) and not descended into.
  """
  if os.path.isfile(path):
    yield path, False
    return
  for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
    dirnames.sort()
    if zarr.is_zarr_store(dirpath):
      dirnames[:] = []
      yield dirpath, True
      continue
    for fname in sorted(filenames):
      yield os.path.join(dirpath, fname), False
    if not recursive:
      break


def _check_target(target, is_zarr):
  """Return the file report for one target, never raising.

  A path that resolves outside /glade/ (e.g. a symlink pointing elsewhere) is
  skipped with a warning rather than opened.
  """
  try:
    glade_path_validate(target)
  except ValueError as e:
    return base.file_report(target, None, "skipped", [base.finding(base.OUTSIDE_ROOT, base.WARNING, str(e))])
  if is_zarr:
    return zarr.check(target)
  try:
    return check_file(target)
  except Exception as e:  # a checker bug or odd file must not abort the scan
    return base.file_report(target, None, "checked", [base.finding(base.UNREADABLE, base.ERROR, f"Check crashed: {e}")])


def _scan_files(path, report):
  """Step 2: check every file under the path and summarise the outcome.

  Result is True only if at least one file was scanned and none has an error.
  False is not a task failure (job status stays SUCCESS, like check-data's
  check_readable): read details.compliant. An empty directory is False, since
  there was nothing to certify.

  Details:
    {"compliant": bool, "files_scanned": N, "files_checked": N, "files_failed": N,
     "files_with_warnings": N, "files_unsupported": N, "files_skipped": N,
     "failed_files": [{"path", "errors": [...], "warnings": [...]}, ...],   # first 20
     "failed_files_truncated": bool,
     "warning_files": [{"path", "warnings": [...]}, ...],                   # first 20
     "warning_files_truncated": bool}
  files_with_warnings counts checked files with at least one warning (failed
  ones included); warning_files lists only those that otherwise pass.
  Unsupported and skipped files never fail the scan; they are counted separately.
  Every file's findings are written to the job log, one line per file.
  """
  counts = {"files_scanned": 0, "files_checked": 0, "files_failed": 0,
            "files_with_warnings": 0, "files_unsupported": 0, "files_skipped": 0}
  failed, warned = [], []
  for target, is_zarr in _iter_targets(path, report.get("recursive", True)):
    result = _check_target(target, is_zarr)
    counts["files_scanned"] += 1
    status, errors, warnings = result["status"], result["errors"], result["warnings"]
    if status == "unsupported":
      counts["files_unsupported"] += 1
    elif status == "skipped":
      counts["files_skipped"] += 1
    else:
      counts["files_checked"] += 1
      counts["files_with_warnings"] += bool(warnings)
      if errors:
        counts["files_failed"] += 1
        failed.append({"path": target, "errors": errors, "warnings": warnings})
      elif warnings:
        warned.append({"path": target, "warnings": warnings})
    level = "ERROR" if errors else "WARNING" if warnings else "INFO"
    _log(report, "scan_files", level, f"{target}: {'non-compliant' if errors else status}",
         path=target, format=result["format"], status=status, errors=errors, warnings=warnings)

  compliant = counts["files_scanned"] > 0 and counts["files_failed"] == 0
  details = {
      "compliant": compliant,
      **counts,
      "failed_files": failed[:MAX_LISTED_FILES],
      "failed_files_truncated": len(failed) > MAX_LISTED_FILES,
      "warning_files": warned[:MAX_LISTED_FILES],
      "warning_files_truncated": len(warned) > MAX_LISTED_FILES,
  }
  return compliant, details


#####################################################################
# 2. Tasks
#
# The Celery tasks that run the steps above, as a chain:
#   validate_path_task -> scan_files_task
# Each task passes the same step report to the next and fills in its own
# entry. The last task's return value is the job's result.
#
# submit_compliance (at the end of this section) builds and queues the chain.
#####################################################################


@celery_app.task
def validate_path_task(path, recursive=True):
  """First task in the compliance chain: start the step report and log, and run validate_path.

  recursive is stored on the report (report["recursive"]) for scan_files to read.

  Success: {"path": path, "log_url": null, "recursive": true, "tasks": {"validate_path":
            {"status": "SUCCESS", "result": true, "error": null, "details": {"is_directory": true | false}},
            "scan_files": <SKIPPED>}, "_log": {...}}
  Failure: validate_path is {"status": "FAILURE", "error": "<message>", ...} and scan_files stays SKIPPED.
  """
  report = new_report(path, COMPLIANCE_STEPS, "check-compliance")
  report["recursive"] = recursive
  return run_step(report, "validate_path", _validate_path)


@celery_app.task(bind=True)
def scan_files_task(self, report):
  """Last task: run scan_files if validate_path succeeded, else leave it SKIPPED.

  Its return value is the job's result, sent to the callback and returned by
  GET /celery/task-status/<external_job_id>. It also uploads the finished log
  under its own task id (the job's external_job_id), sets log_url, and removes
  "_log" from the report.

  Success: "scan_files": {"status": "SUCCESS", "result": true | false, "error": null,
           "details": <see _scan_files>}
  Failure: "scan_files": {"status": "FAILURE", "result": null, "error": "<message>", "details": null}
  """
  return run_step(report, "scan_files", _scan_files, external_job_id=self.request.id)


def submit_compliance(path, recursive=True, callback_url=None, background_tasks=None):
  """Queue the compliance chain for path; called by POST /files/check-compliance.

  Builds validate_path_task -> scan_files_task here, so the router doesn't need
  to know about Celery or the steps. See common.submit_portal_task for
  callback_url and background_tasks.

  Returns (before any task runs):
    {"external_job_id": "<id>", "status": "SUBMITTED", "result": null}
  """
  workflow = chain(validate_path_task.s(path, recursive), scan_files_task.s())
  return submit_portal_task(workflow, callback_url, background_tasks)
