"""Celery tasks that run directly in the worker pod (no PBS submission)."""
from app.celery_app import celery_app
from pathlib import Path
import requests


#####################################
######## Shared Tasks ###############
#####################################


def job_response(task_id, status, result=None, portal_task_id=None):
  """Fields returned for every portal task, in API responses and callback payloads.

  result holds the task's return value on success, or the error message on failure.
  """
  return {"external_job_id": task_id, "portal_task_id": portal_task_id, "status": status, "result": result}


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
  response = requests.post(url, json=payload)
  if response.ok:
      print("Success")
      print(response.json())
  else:
      print(f"Failed with status code: {response.status_code}")


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
def check_recursive_task(path):
  is_url = path.startswith("http://") or path.startswith("https://")
  if is_url:
      # Raising marks the task FAILURE; Celery propagates it to compute_size_task in the chain
      raise ValueError(f"The provided endpoint is a URL, but this function only supports endpoint paths on /glade: {path}")

  p = Path(path)
  if not p.exists():
      raise FileNotFoundError(f"Path does not exist: {path}")
  return {"path": path, "is_dir": p.is_dir()}


@celery_app.task
def compute_size_task(check_result):
  path = check_result["path"]
  p = Path(path)

  if check_result["is_dir"]:
      total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
      return {"path": path, "is_directory": True, "size_bytes": total}

  return {"path": path, "is_directory": False, "size_bytes": p.stat().st_size}