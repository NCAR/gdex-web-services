"""Celery tasks for the gdex-web-portal, run directly in the worker pod (no PBS submission).

How a portal job works, start to finish:

  1. An API endpoint (e.g. GET /files/check-data) calls a submit function, which
     returns the job's id to the client right away:
       {"external_job_id": "<id>", "status": "SUBMITTED", "result": null}
  2. After that response is sent, the job is queued in Redis and a Celery worker
     runs it. A job is one task, or a chain of tasks run one after another.
  3. When the job finishes, the result is POSTed to the caller's callback URL (if
     one was given) and can also be fetched from GET /celery/task-status/<id>
     (defined in app/routers/celery.py; see common.py, section 1).

Modules:

  common.py      shared by every portal job: response format, submitting jobs,
                 callbacks, and step reports
  check_data.py  GET /files/check-data: validate_path -> check_readable -> data_size
  smoke.py       GET /celery/test-celery: checks the worker is up

Adding a new portal endpoint (see check_data.py as the example):

  1. Create app/tasks/portal/<endpoint>.py. List its step names in order, and
     write one plain function per step: step(path, report) -> (result, details).
     Raise on errors; run_step records them as FAILURE.
  2. Wrap each step in a Celery task that calls common.run_step. The first task
     starts the report with common.new_report.
  3. Add submit_<endpoint>(...), which chains the tasks and calls
     common.submit_portal_task. The router calls only this function.

No change to app/celery_app.py is needed: its include lists this package, and
the import below loads every module in this folder, so the worker registers
the new module's tasks automatically.

Celery names each task by its module path (e.g.
app.tasks.portal.check_data.validate_path_task). Moving or renaming a task
changes that name, so do it between deploys, not while jobs are queued.
"""
import importlib
import pkgutil

# Import every module in this package so Celery registers their tasks
for _module in pkgutil.iter_modules(__path__):
  importlib.import_module(f"{__name__}.{_module.name}")
