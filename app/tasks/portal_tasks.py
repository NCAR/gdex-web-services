"""Celery tasks that run directly in the worker pod (no PBS submission)."""
from app.celery_app import celery_app
from app.utils.callback import post_to_callback_url
from pathlib import Path
import os
import stat


#####################################
######## Shared Tasks ###############
#####################################


def job_response(task_id, status, result=None):
  """Fields returned for every portal task, in API responses and callback payloads.

  result holds the task's return value (for a chain, a step report: see _run_step),
  or the error message if the task crashed outside a step.
  """
  return {"external_job_id": task_id, "status": status, "result": result}


def job_status(celery_status, result):
  """The status to report: FAILURE if any step in a step report failed, else Celery's status.

  Steps record their own failures rather than raising (see _run_step), so Celery
  marks the chain SUCCESS even when a step failed.
  """
  steps = result.get("tasks", {}).values() if isinstance(result, dict) else []
  return "FAILURE" if any(s["status"] == "FAILURE" for s in steps) else celery_status


def submit_portal_task(sig, callback_url=None, background_tasks=None):
  """Queue a portal task or chain, with an optional callback URL.

  Pass the endpoint's FastAPI BackgroundTasks to queue the task only after the
  response (with its ids) has been sent, so no task runs before the client has
  its id. Without it, the task is queued immediately.
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


@celery_app.task
def notify_callback_success(result, callback_url, task_id):
  post_to_callback_url(callback_url, job_response(task_id, job_status("SUCCESS", result), result))


@celery_app.task
def notify_callback_failure(request, exc, traceback, callback_url, task_id):
  # Celery calls errbacks with (request, exc, traceback) ahead of the signature's own args
  post_to_callback_url(callback_url, job_response(task_id, "FAILURE", str(exc)))


#####################################
######## /celery Tasks ##############
#####################################

@celery_app.task
def test_task():
  print("Hello from Celery!")
  return "Yo, Celery is working!"

#####################################
######## /router/files Tasks ########
#####################################

CHECK_DATA_STEPS = ("validate_path", "check_readable", "data_size")


def _step_entry(status, result=None, error=None, details=None):
  return {"status": status, "result": result, "error": error, "details": details}


def _new_report(path, steps):
  """Start a step report with every step SKIPPED, so the shape is complete however far the chain gets."""
  return {"path": path, "tasks": {name: _step_entry("SKIPPED") for name in steps}}


def _run_step(report, name, step):
  """Run one step of a chain and record its outcome under report["tasks"][name].

  The step only runs if every earlier step succeeded with result True; otherwise
  it stays SKIPPED. step(path, report) returns (result, details). An exception
  is recorded as FAILURE with its message rather than raised, so later steps
  still run (and skip) and the chain always finishes with a complete report.
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


def _validate_path(path, report):
  if path.startswith("http://") or path.startswith("https://"):
      raise ValueError(f"The provided endpoint is a URL, but this function only supports endpoint paths on /glade: {path}")
  p = Path(path)
  if not p.exists():
      raise FileNotFoundError(f"Path does not exist: {path}")
  return True, {"is_directory": p.is_dir()}


def _others_can_reach(path):
  """True if every directory above `path` lets other users traverse into it (o+x)."""
  return all(parent.stat().st_mode & stat.S_IXOTH for parent in path.resolve().parents)


def _others_can_list(mode):
  """True if a directory with this mode can be listed and entered by other users (o+rx)."""
  return bool(mode & stat.S_IROTH and mode & stat.S_IXOTH)


def _check_readable(path, report):
  """Check that every file under the path is readable by other users.

  A file counts as unreadable if it lacks o+r, or if any directory above it
  lacks o+rx (or o+x above the submitted path). Unreadable files are grouped
  by the directory that holds them. Directories the worker itself can't list
  are reported in unscanned_directories, since their files can't be checked.
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


def _data_size(path, report):
  """Total the path's size in bytes."""
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


@celery_app.task
def validate_path_task(path):
  return _run_step(_new_report(path, CHECK_DATA_STEPS), "validate_path", _validate_path)


@celery_app.task
def check_readable_task(report):
  return _run_step(report, "check_readable", _check_readable)


@celery_app.task
def data_size_task(report):
  return _run_step(report, "data_size", _data_size)