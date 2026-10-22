"""GET /files/check-data: validate a /glade path, check it's readable by others, and size it.

The steps run as a Celery chain, in order:
  validate_path   is it a local path that exists?
  check_readable  can other users read every file under it?
  data_size       how many bytes is it? (only if everything is readable)

This file is laid out in two sections:

  1. Steps   the plain functions that do the work
  2. Tasks   the Celery tasks that run each step, and submit_check_data,
             which builds and queues the chain
"""
from app.celery_app import celery_app
from app.tasks.portal.common import new_report, run_step, submit_portal_task
from celery import chain
from pathlib import Path
import os
import stat


#####################################################################
# 1. Steps
#
# Each step is step(path, report) -> (result, details). A step runs only
# if every earlier step returned True (see common.run_step).
#####################################################################

CHECK_DATA_STEPS = ("validate_path", "check_readable", "data_size")


def _validate_path(path, report):
  """Step 1: check the path is a local path (not a URL) that exists.

  Success: returns (True, {"is_directory": true | false}).
  Failure: raises ValueError for a URL ("The provided endpoint is a URL, ...") or
  FileNotFoundError ("Path does not exist: <path>"); run_step records it as FAILURE.
  """
  if path.startswith("http://") or path.startswith("https://"):
      raise ValueError(f"The provided endpoint is a URL, but this function only supports endpoint paths on /glade: {path}")
  p = Path(path)
  if not p.exists():
      raise FileNotFoundError(f"Path does not exist: {path}")
  return True, {"is_directory": p.is_dir()}


def _check_readable(path, report):
  """Check that every file under the path is readable by other users.

  A file counts as unreadable if it lacks o+r, or if any directory above it
  lacks o+rx (or o+x above the submitted path). Unreadable files are grouped
  by the directory that holds them. Directories the worker itself can't list
  are reported in unscanned_directories, since their files can't be checked.

  Step 2. Returns (result, details), where result is True only if no file is
  unreadable and no directory was unscanned. Unreadable files give False, which
  is not a failure:
    (True | False, {"file_count": int, "unreadable_file_count": int,
                    "unreadable_directories": [{"directory": "<dir>", "unreadable_files": int}, ...],
                    "unscanned_directories": ["<dir>", ...]})
  Failure: an OS error (e.g. a parent directory can't be stat'ed) is raised and
  run_step records it as FAILURE.
  """
  p = Path(path)
  reachable = _others_can_reach(p)
  unreadable = {}  # directory -> number of unreadable files in it
  unscanned = []
  file_count = 0

  if report["tasks"]["validate_path"]["details"]["is_directory"]:
      dir_ok = {}  # directory -> whether others can get into it
      for dirpath, _dirnames, filenames in os.walk(p, followlinks=False, onerror=lambda e: unscanned.append(e.filename)):
          ok = dir_ok.get(os.path.dirname(dirpath), reachable) and _others_can_list(os.stat(dirpath).st_mode)
          dir_ok[dirpath] = ok
          for fname in filenames:
              try:
                  mode = os.stat(os.path.join(dirpath, fname)).st_mode
              except OSError:
                  continue  # vanished or a broken symlink
              file_count += 1
              if not (ok and mode & stat.S_IROTH):
                  unreadable[dirpath] = unreadable.get(dirpath, 0) + 1
  else:
      file_count = 1
      if not (reachable and p.stat().st_mode & stat.S_IROTH):
          unreadable[str(p.parent)] = 1

  return not unreadable and not unscanned, {
      "file_count": file_count,
      "unreadable_file_count": sum(unreadable.values()),
      "unreadable_directories": [{"directory": d, "unreadable_files": n} for d, n in sorted(unreadable.items())],
      "unscanned_directories": sorted(unscanned),
  }


def _others_can_reach(path):
  """True if every directory above `path` lets other users traverse into it (o+x)."""
  return all(parent.stat().st_mode & stat.S_IXOTH for parent in path.resolve().parents)


def _others_can_list(mode):
  """True if a directory with this mode can be listed and entered by other users (o+rx)."""
  return bool(mode & stat.S_IROTH and mode & stat.S_IXOTH)


def _data_size(path, report):
  """Step 3: total the path's size in bytes.

  Only runs when check_readable returned True. Files that vanish mid-walk are skipped.
  Success: returns (<size in bytes>, None).
  Failure: an OS error (e.g. the single file can't be stat'ed) is raised and
  run_step records it as FAILURE.
  """
  p = Path(path)
  if not report["tasks"]["validate_path"]["details"]["is_directory"]:
      return p.stat().st_size, None

  total = 0
  for dirpath, _dirnames, filenames in os.walk(p, followlinks=False):
      for fname in filenames:
          try:
              total += os.stat(os.path.join(dirpath, fname)).st_size
          except OSError:
              continue
  return total, None


#####################################################################
# 2. Tasks
#
# The Celery tasks that run the steps above, as a chain:
#   validate_path_task -> check_readable_task -> data_size_task
# Each task passes the same step report to the next and fills in its own
# entry. The last task's return value is the job's result.
#
# submit_check_data (at the end of this section) builds and queues the chain.
#####################################################################


@celery_app.task
def validate_path_task(path):
  """First task in the check-data chain: start the step report and log, and run validate_path.

  Success: {"path": path, "log_url": null, "tasks": {"validate_path": {"status": "SUCCESS",
            "result": true, "error": null, "details": {"is_directory": true | false}},
            "check_readable": <SKIPPED>, "data_size": <SKIPPED>}, "_log": {...}}
  Failure: {"path": path, "log_url": null, "tasks": {"validate_path": {"status": "FAILURE",
            "result": null, "error": "Path does not exist: <path>", "details": null},
            "check_readable": <SKIPPED>, "data_size": <SKIPPED>}, "_log": {...}}
  """
  return run_step(new_report(path, CHECK_DATA_STEPS, "check-data"), "validate_path", _validate_path)


@celery_app.task
def check_readable_task(report):
  """Second task: run check_readable if validate_path succeeded, else leave it SKIPPED.

  Success (all readable):  "check_readable": {"status": "SUCCESS", "result": true, "error": null,
                           "details": {"file_count": 2, "unreadable_file_count": 0,
                           "unreadable_directories": [], "unscanned_directories": []}}
  Success (some unreadable): "result": false, with the directories and counts in details.
  Failure:                 "check_readable": {"status": "FAILURE", "result": null,
                           "error": "<message>", "details": null}
  Returns the whole report, with data_size still SKIPPED.
  """
  return run_step(report, "check_readable", _check_readable)


@celery_app.task(bind=True)
def data_size_task(self, report):
  """Last task: run data_size if validate_path and check_readable both returned True.

  Its return value is the job's result, sent to the callback and returned by
  GET /celery/task-status/<external_job_id>. It also adds the job's last log
  line, uploads the finished log under its own task id (the job's
  external_job_id), sets log_url, and removes "_log" from the report.

  Success: "data_size": {"status": "SUCCESS", "result": <size in bytes>, "error": null, "details": null}
  Failure: "data_size": {"status": "FAILURE", "result": null, "error": "<message>", "details": null}
  Skipped (an earlier step failed or returned False): "data_size": {"status": "SKIPPED", ...all null}
  """
  return run_step(report, "data_size", _data_size, job_id=self.request.id)


def submit_check_data(path, callback_url=None, background_tasks=None):
  """Queue the check-data chain for path; called by GET /files/check-data.

  Builds validate_path_task -> check_readable_task -> data_size_task here, so
  the router doesn't need to know about Celery or the steps (the same split as
  transform_tasks.celery_transform). See common.submit_portal_task for
  callback_url and background_tasks.

  Returns (before any task runs):
    {"external_job_id": "<id>", "status": "SUBMITTED", "result": null}
  """
  workflow = chain(validate_path_task.s(path), check_readable_task.s(), data_size_task.s())
  return submit_portal_task(workflow, callback_url, background_tasks)
