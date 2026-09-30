from fastapi import APIRouter
from app.celery_app import celery_app
from app.tasks import test_task, notify_callback_success, notify_callback_failure
from celery.result import AsyncResult
from celery.utils import uuid as celery_uuid

router = APIRouter(prefix="/celery", tags=["celery"])


@router.get("/test-celery")
def test_celery(callback_url: str | None = None):
    task_id = celery_uuid()
    kwargs = {"task_id": task_id}

    if callback_url:
        kwargs["link"] = notify_callback_success.s(callback_url, task_id)
        kwargs["link_error"] = notify_callback_failure.s(callback_url, task_id)

    task = test_task.apply_async(**kwargs)

    return {
        "task_id": task.id,
        "status": "SUBMITTED",
        "callback_url": callback_url
    }


@router.get("/task-status/{task_id}")
def task_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)

    if result.failed():
        return {
            "task_id": task_id,
            "task_type": "celery",  # celery vs pbs
            "status": result.status,
            "result": str(result.result),
            "traceback": result.traceback,
        }

    return {
        "task_id": task_id,
        "task_type": "celery", # celery vs pbs
        "status": result.status,   # PENDING, STARTED, SUCCESS, FAILURE, etc.
        "result": result.result if result.ready() else None,
    }