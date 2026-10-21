"""Celery tasks for the gdex-web-portal, run directly in the worker pod (no PBS submission).

How a portal job works, start to finish:

  1. An API endpoint (e.g. GET /files/check-data) calls submit_portal_task, which
     returns the job's id to the client right away:
       {"external_job_id": "<id>", "status": "SUBMITTED", "result": null}
  2. After that response is sent, the job is queued in Redis and a Celery worker
     runs it. A job is one task, or a chain of tasks run one after another.
  3. When the job finishes, the result is POSTed to the caller's callback URL (if
     one was given) and can also be fetched from GET /celery/task-status/<id>.

This file is laid out in that order:

  1. Job responses       the format every API response and callback uses
  2. Submitting jobs     queueing a job and returning its id
  3. Callbacks           telling the caller the job is done
  4. Step reports        how a chain of steps records each step's outcome
  5. Check-data steps    the work behind GET /files/check-data
  6. Check-data tasks    the Celery tasks that run those steps, in order
  7. Test task           behind GET /celery/test-celery
"""
from app.celery_app import celery_app
from app.utils.callback import post_to_callback_url
from pathlib import Path
import os
import stat


#####################################################################
# 1. Job responses
#
# Every API response and callback for a portal job has the same three
# fields: external_job_id, status and result.
#####################################################################


def job_response(task_id, status, result=None):
  """Fields returned for every portal task, in API responses and callback payloads.

  result holds the task's return value (for a chain, a step report: see _run_step),
  or the error message if the task crashed outside a step.

  Returns:
    {"external_job_id": "<celery task id>", "status": "SUBMITTED" | "PENDING" | "STARTED"
     | "SUCCESS" | "FAILURE", "result": <step report, string, or null>}
  """
  return {"external_job_id": task_id, "status": status, "result": result}


def job_status(celery_status, result):
  """The status to report: FAILURE if any step in a step report failed, else Celery's status.

  Steps record their own failures rather than raising (see _run_step), so Celery
  marks the chain SUCCESS even when a step failed.
  """
  steps = result.get("tasks", {}).values() if isinstance(result, dict) else []
  return "FAILURE" if any(s["status"] == "FAILURE" for s in steps) else celery_status


#####################################################################
# 2. Submitting jobs
#####################################################################


def submit_portal_task(sig, callback_url=None, background_tasks=None):
  """Queue a portal task or chain, with an optional callback URL.

  Pass the endpoint's FastAPI BackgroundTasks to queue the task only after the
  response (with its ids) has been sent, so no task runs before the client has
  its id. Without it, the task is queued immediately.

  With a callback_url, notify_callback_success runs after the last task returns,
  and notify_callback_failure runs if a task crashes outside a step.

  Returns (before any task runs):
    {"external_job_id": "<id of the last task in the chain>", "status": "SUBMITTED", "result": null}
  """
  # freeze() assigns the task ids now, so they can go in the response and callbacks
  task_id = sig.freeze().id
  if callback_url:
      sig.link(notify_callback_success.s(callback_url, task_id))
      sig.link_error(notify_callback_failure.s(callback_url, task_id))
  if background_tasks is not None:
      background_tasks.add_task(sig.apply_async)
  else:
      sig.apply_async()
  return job_response(task_id, "SUBMITTED")


#####################################################################
# 3. Callbacks
#
# Celery runs one of these after the job ends, if a callback_url was given.
#####################################################################


@celery_app.task
def notify_callback_success(result, callback_url, task_id):
  """POST the finished job to callback_url once the last task returns.

  Runs whenever the chain finishes, including when a step recorded a FAILURE, so
  status comes from job_status: SUCCESS, or FAILURE if any step failed.

  Posts: {"external_job_id": task_id, "status": "SUCCESS" | "FAILURE", "result": <step report>}
  """
  post_to_callback_url(callback_url, job_response(task_id, job_status("SUCCESS", result), result))


@celery_app.task
def notify_callback_failure(request, exc, traceback, callback_url, task_id):
  """POST a crash to callback_url when a task raised instead of finishing.

  Steps catch their own errors (see _run_step), so this only runs for a crash
  outside a step, e.g. the worker failing to deserialize the task.

  Posts: {"external_job_id": task_id, "status": "FAILURE", "result": "<error message>"}
  """
  # Celery calls errbacks with (request, exc, traceback) ahead of the signature's own args
  post_to_callback_url(callback_url, job_response(task_id, "FAILURE", str(exc)))


#####################################################################
# 4. Step reports
#
# A job made of several steps passes one "step report" from task to task.
# Each step fills in its own entry, so the final result shows what every
# step did:
#
#   {"path": "<path>", "tasks": {
#      "<step name>": {"status": "SUCCESS" | "FAILURE" | "SKIPPED",
#                      "result": ..., "error": ..., "details": ...},
#      ...}}
#
# A step only runs if every step before it succeeded with result True.
#####################################################################


def _step_entry(status, result=None, error=None, details=None):
  """One step's entry in a step report.

  Returns:
    {"status": "SUCCESS" | "FAILURE" | "SKIPPED", "result": ..., "error": "<message>" | null,
     "details": {...} | null}
  """
  return {"status": status, "result": result, "error": error, "details": details}


def _new_report(path, steps):
  """Start a step report with every step SKIPPED, so the shape is complete however far the chain gets.

  Returns:
    {"path": path, "tasks": {"validate_path": <SKIPPED entry>, "check_readable": <SKIPPED entry>,
     "data_size": <SKIPPED entry>}}
  """
  return {"path": path, "tasks": {name: _step_entry("SKIPPED") for name in steps}}


def _run_step(report, name, step):
  """Run one step of a chain and record its outcome under report["tasks"][name].

  The step only runs if every earlier step succeeded with result True; otherwise
  it stays SKIPPED. step(path, report) returns (result, details). An exception
  is recorded as FAILURE with its message rather than raised, so later steps
  still run (and skip) and the chain always finishes with a complete report.

  Sets report["tasks"][name] to one of:
    Success: {"status": "SUCCESS", "result": <step result>, "error": null, "details": <step details>}
    Failure: {"status": "FAILURE", "result": null, "error": "<exception message>", "details": null}
    Skipped: {"status": "SKIPPED", "result": null, "error": null, "details": null}
  and returns the report.
  """
  tasks = report["tasks"]
  earlier = list(tasks)[:list(tasks).index(name)]
  if all(tasks[n]["status"] == "SUCCESS" and tasks[n]["result"] is True for n in earlier):
      try:
          result, details = step(report["path"], report)
          tasks[name] = _step_entry("SUCCESS", result, details=details)
      except Exception as e:
          tasks[name] = _step_entry("FAILURE", error=str(e))
  return report


#####################################################################
# 5. Check-data steps (GET /files/check-data)
#
# The checks behind GET /files/check-data, in the order they run:
#   validate_path   is it a local path that exists?
#   check_readable  can other users read every file under it?
#   data_size       how many bytes is it? (only if everything is readable)
#####################################################################

CHECK_DATA_STEPS = ("validate_path", "check_readable", "data_size")


def _validate_path(path, report):
  """Step 1: check the path is a local path (not a URL) that exists.

  Success: returns (True, {"is_directory": true | false}).
  Failure: raises ValueError for a URL ("The provided endpoint is a URL, ...") or
  FileNotFoundError ("Path does not exist: <path>"); _run_step records it as FAILURE.
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
  _run_step records it as FAILURE.
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
  _run_step records it as FAILURE.
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
# 6. Check-data tasks (GET /files/check-data)
#
# The Celery tasks that run the steps above, as a chain:
#   validate_path_task -> check_readable_task -> data_size_task
# Each task passes the same step report to the next and fills in its own
# entry. The last task's return value is the job's result.
#####################################################################


@celery_app.task
def validate_path_task(path):
  """First task in the check-data chain: start the step report and run validate_path.

  Success: {"path": path, "tasks": {"validate_path": {"status": "SUCCESS", "result": true,
            "error": null, "details": {"is_directory": true | false}},
            "check_readable": <SKIPPED>, "data_size": <SKIPPED>}}
  Failure: {"path": path, "tasks": {"validate_path": {"status": "FAILURE", "result": null,
            "error": "Path does not exist: <path>", "details": null},
            "check_readable": <SKIPPED>, "data_size": <SKIPPED>}}
  """
  return _run_step(_new_report(path, CHECK_DATA_STEPS), "validate_path", _validate_path)


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
  return _run_step(report, "check_readable", _check_readable)


@celery_app.task
def data_size_task(report):
  """Last task: run data_size if validate_path and check_readable both returned True.

  Its return value is the job's result, sent to the callback and returned by
  GET /celery/task-status/<external_job_id>.

  Success: "data_size": {"status": "SUCCESS", "result": <size in bytes>, "error": null, "details": null}
  Failure: "data_size": {"status": "FAILURE", "result": null, "error": "<message>", "details": null}
  Skipped (an earlier step failed or returned False): "data_size": {"status": "SKIPPED", ...all null}
  """
  return _run_step(report, "data_size", _data_size)


#####################################################################
# 7. Test task (GET /celery/test-celery)
#####################################################################


@celery_app.task
def test_task():
  """Check that the worker is up: returns a fixed string as the job's result."""
  print("Hello from Celery!")
  return "Yo, Celery is working!"
