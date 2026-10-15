"""Celery tasks that run directly in the worker pod (no PBS submission)."""
from app.celery_app import celery_app
from pathlib import Path
import os
import stat
import requests


#####################################
######## Shared Tasks ###############
#####################################


def job_response(task_id, status, result=None, portal_task_id=None):
  """Fields returned for every portal task, in API responses and callback payloads.

  result holds the task's return value on success, or the error message on failure.
  """
  return {"external_job_id": task_id, "id": portal_task_id, "status": status, "result": result}


def _portal_task_key(task_id):
  return f"portal-task-id-{task_id}"


def save_portal_task_id(task_id, portal_task_id):
  """Record the portal's UUID in the result backend (Redis DB 1), next to celery-task-meta-<task_id>.

  Kept as its own key rather than a task stamp: a failed chain writes the final task's
  FAILURE record without stamps. Expires along with the task result (result_expires).
  """
  celery_app.backend.set(_portal_task_key(task_id), str(portal_task_id))


def get_portal_task_id(task_id):
  value = celery_app.backend.get(_portal_task_key(task_id))
  return value.decode() if isinstance(value, bytes) else value


def submit_portal_task(sig, callback_url=None, portal_task_id=None):
  """Queue a portal task or chain, with an optional callback URL and portal UUID."""
  # freeze() assigns the task ids now, so the UUID is saved before any task can finish
  task_id = sig.freeze().id
  if portal_task_id:
      save_portal_task_id(task_id, portal_task_id)
  if callback_url:
      sig.link(notify_callback_success.s(callback_url, task_id, portal_task_id))
      sig.link_error(notify_callback_failure.s(callback_url, task_id, portal_task_id))
  sig.apply_async()
  return job_response(task_id, "SUBMITTED", portal_task_id=portal_task_id)


def _post_to_callback_url(url, payload):
  # The portal rejects callbacks without its shared secret (gdex-services-callback-secret)
  headers = {"X-GDEX-Services-Secret": os.environ.get("GDEX_SERVICES_CALLBACK_SECRET", "")}
  response = requests.post(url, json=payload, headers=headers)
  if response.ok:
      print("Success")
      print(response.json())
  else:
      # The portal explains rejections in a JSON "message" (e.g. secret missing or
      # mismatched, unknown task id); fall back to the raw body for non-JSON errors
      try:
          reason = response.json().get("message", response.text)
      except (ValueError, AttributeError):
          reason = response.text[:500]
      print(f"Callback to {url} failed with status code {response.status_code}: {reason}")


@celery_app.task
def notify_callback_success(result, callback_url, task_id, portal_task_id=None):
  _post_to_callback_url(callback_url, job_response(task_id, "SUCCESS", result, portal_task_id))


@celery_app.task
def notify_callback_failure(request, exc, traceback, callback_url, task_id, portal_task_id=None):
  # Celery calls errbacks with (request, exc, traceback) ahead of the signature's own args
  _post_to_callback_url(callback_url, job_response(task_id, "FAILURE", str(exc), portal_task_id))


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


@celery_app.task
def validate_path_task(path):
  is_url = path.startswith("http://") or path.startswith("https://")
  if is_url:
      # Raising marks the task FAILURE; Celery propagates it down the rest of the chain
      raise ValueError(f"The provided endpoint is a URL, but this function only supports endpoint paths on /glade: {path}")

  p = Path(path)
  if not p.exists():
      raise FileNotFoundError(f"Path does not exist: {path}")
  return {"path": path, "is_directory": p.is_dir()}


def _others_can_reach(path):
  """True if every directory above `path` lets other users traverse into it (o+x)."""
  return all(parent.stat().st_mode & stat.S_IXOTH for parent in path.resolve().parents)


def _others_can_list(mode):
  """True if a directory with this mode can be listed and entered by other users (o+rx)."""
  return bool(mode & stat.S_IROTH and mode & stat.S_IXOTH)


@celery_app.task
def check_readable_task(check_result):
  """Check that every file under the path is readable by other users.

  A file counts as unreadable if it lacks o+r, or if any directory above it
  lacks o+rx (or o+x above the submitted path). Unreadable files are grouped
  by the directory that holds them. Directories the worker itself can't list
  are reported in unscanned_directories, since their files can't be checked.
  """
  path = check_result["path"]
  p = Path(path)
  reachable = _others_can_reach(p)
  unreadable = {}  # directory -> number of unreadable files in it
  unscanned = []
  file_count = 0

  if check_result["is_directory"]:
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

  return {
      **check_result,
      "globally_readable": not unreadable and not unscanned,
      "file_count": file_count,
      "unreadable_file_count": sum(unreadable.values()),
      "unreadable_directories": [{"directory": d, "unreadable_files": n} for d, n in sorted(unreadable.items())],
      "unscanned_directories": sorted(unscanned),
  }


@celery_app.task
def compute_size_task(check_result):
  """Total the path's size in bytes, only once check_readable_task found it all readable."""
  if not check_result["globally_readable"]:
      return {**check_result, "size_bytes": None}

  p = Path(check_result["path"])
  if not check_result["is_directory"]:
      return {**check_result, "size_bytes": p.stat().st_size}

  total = 0
  for dirpath, _dirnames, filenames in os.walk(p, followlinks=False):
      for fname in filenames:
          try:
              total += os.stat(os.path.join(dirpath, fname)).st_size
          except OSError:
              continue
  return {**check_result, "size_bytes": total}