"""
Celery application for gdex-web-services.

The broker (Redis DB 0) carries the task queue; the result backend (Redis DB 1)
holds task state, which GET /compose/status/{request_id} reads. Both URLs are
injected via environment variables (see app-chart/templates/deployment*.yaml).
"""
import os
from celery import Celery

broker_url = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/1")

celery_app = Celery(
    "gdex_web_services",
    broker=broker_url,
    backend=result_backend,
    include=["app.tasks.transform_tasks", "app.tasks.callback_task", "app.tasks.portal"]
    # app.tasks.portal loads every module in its folder (see its __init__.py)
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    result_extended=True,
    # Redeliver a task if the worker dies mid-execution, instead of losing it.
    task_acks_late=True,
    # Retry a failed publish (Redis blip) before apply_async raises. Celery's default is
    # 3 tries over ~0.6s; this is ~7s, still short enough for the API request to wait on.
    # A republish reuses the same task_id, so a rare duplicate message is harmless.
    task_publish_retry=True,
    task_publish_retry_policy={
        "max_retries": 6,
        "interval_start": 0.2,
        "interval_step": 0.4,
        "interval_max": 2,
    },
    worker_prefetch_multiplier=1,
    # Scheduled retries / watch_job polls (countdown=...) live in Redis, so
    # Redis must run with persistence (appendonly) enabled.
)
