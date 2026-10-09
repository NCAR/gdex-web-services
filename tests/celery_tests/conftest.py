"""Pytest setup for the Celery-related tests only (this folder).

Runs Celery tasks eagerly (in-process) with an in-memory result backend so
no Redis is needed. Lives here, not in tests/conftest.py, so tests outside this
folder (e.g. the gdexws tests in CI, where Celery is not installed) never import it.
"""
from app.celery_app import celery_app

celery_app.conf.update(
    broker_url="memory://",
    result_backend="cache+memory://",
    task_always_eager=True,
    task_store_eager_result=True,
)
