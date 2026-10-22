"""GET /celery/test-celery: a one-task portal job that checks the worker is up."""
from app.celery_app import celery_app


@celery_app.task
def test_task():
  """Check that the worker is up: returns a fixed string as the job's result."""
  print("Hello from Celery!")
  return "Yo, Celery is working!"
