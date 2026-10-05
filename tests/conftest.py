"""Shared pytest setup.

Runs Celery tasks eagerly (in-process) with an in-memory result backend so
no Redis is needed. Must happen before app modules that enqueue tasks are used.
"""
from app.celery_app import celery_app

celery_app.conf.update(
    broker_url="memory://",
    result_backend="cache+memory://",
    task_always_eager=True,
    task_store_eager_result=True,
)
