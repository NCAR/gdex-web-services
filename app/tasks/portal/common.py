"""Shared machinery for every portal job (see app/tasks/portal/__init__.py for the overview).

This file is laid out in the order a job uses it:

  1. Job responses       the format every API response and callback uses
  2. Submitting jobs     queueing a job and returning its id
  3. Callbacks           telling the caller the job is done
  4. Step reports        how a chain of steps records each step's outcome
"""
import httpx
from app.celery_app import celery_app
from app.utils.callback import callback_to_portal


#####################################################################
# 1. Job responses
#
# Every API response and callback for a portal job has the same three
# fields: external_job_id, status and result.
#
# Checking a job's status: GET /celery/task-status/<external_job_id>,
# defined in app/routers/celery.py (task_status). It isn't a Celery task:
# it reads the job's record (celery-task-meta-<id> in Redis DB 1) directly
# in the API request and returns it with the two helpers below:
#   {"external_job_id": "<id>", "status": "PENDING" | "STARTED" | "SUCCESS"
#    | "FAILURE", "result": null until the job finishes}
# An unknown or expired id returns PENDING.
#####################################################################


def job_response(task_id, status, result=None):
  """Fields returned for every portal task, in API responses and callback payloads.

  result holds the task's return value (for a chain, a step report: see run_step),
  or the error message if the task crashed outside a step.

  Returns:
    {"external_job_id": "<celery task id>", "status": "SUBMITTED" | "PENDING" | "STARTED"
     | "SUCCESS" | "FAILURE", "result": <step report, string, or null>}
  """
  return {"external_job_id": task_id, "status": status, "result": result}


def job_status(celery_status, result):
  """The status to report: FAILURE if any step in a step report failed, else Celery's status.

  Steps record their own failures rather than raising (see run_step), so Celery
  marks the chain SUCCESS even when a step failed.
  """
  steps = result.get("tasks", {}).values() if isinstance(result, dict) else []
  return "FAILURE" if any(s["status"] == "FAILURE" for s in steps) else celery_status


#####################################################################
# 2. Submitting jobs
#####################################################################


def submit_portal_task(sig, callback_url=None, background_tasks=None):
  """Queue a portal task or chain, with an optional callback URL.

  Each endpoint module's submit function (e.g. check_data.submit_check_data)
  builds its chain and passes it here.

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


def _send_callback(callback_url, payload):
  """POST payload to callback_url; a portal rejection (4xx/5xx) is printed to the worker log, not raised.

  Nothing is retried. A portal that can't be reached at all still raises httpx.HTTPError.
  """
  try:
    callback_to_portal(callback_url, payload)
  except httpx.HTTPStatusError as e:
    try:
      reason = e.response.json().get("message", e.response.text)
    except (ValueError, AttributeError):
      reason = e.response.text[:500]
    print(f"Callback to {callback_url} failed with status code {e.response.status_code}: {reason}")


@celery_app.task
def notify_callback_success(result, callback_url, task_id):
  """POST the finished job to callback_url once the last task returns.

  Runs whenever the chain finishes, including when a step recorded a FAILURE, so
  status comes from job_status: SUCCESS, or FAILURE if any step failed.

  Posts: {"external_job_id": task_id, "status": "SUCCESS" | "FAILURE", "result": <step report>}
  """
  _send_callback(callback_url, job_response(task_id, job_status("SUCCESS", result), result))


@celery_app.task
def notify_callback_failure(request, exc, traceback, callback_url, task_id):
  """POST a crash to callback_url when a task raised instead of finishing.

  Steps catch their own errors (see run_step), so this only runs for a crash
  outside a step, e.g. the worker failing to deserialize the task.

  Posts: {"external_job_id": task_id, "status": "FAILURE", "result": "<error message>"}
  """
  # Celery calls errbacks with (request, exc, traceback) ahead of the signature's own args
  _send_callback(callback_url, job_response(task_id, "FAILURE", str(exc)))


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
# Endpoint modules start a report with new_report and run each step with
# run_step.
#####################################################################


def _step_entry(status, result=None, error=None, details=None):
  """One step's entry in a step report.

  Returns:
    {"status": "SUCCESS" | "FAILURE" | "SKIPPED", "result": ..., "error": "<message>" | null,
     "details": {...} | null}
  """
  return {"status": status, "result": result, "error": error, "details": details}


def new_report(path, steps):
  """Start a step report with every step SKIPPED, so the shape is complete however far the chain gets.

  steps is the endpoint's step names, in the order they run.

  Returns:
    {"path": path, "tasks": {"<step 1>": <SKIPPED entry>, "<step 2>": <SKIPPED entry>, ...}}
  """
  return {"path": path, "tasks": {name: _step_entry("SKIPPED") for name in steps}}


def run_step(report, name, step):
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
