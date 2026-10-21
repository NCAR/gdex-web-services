from uuid import UUID

from fastapi import APIRouter, BackgroundTasks
from app.celery_app import celery_app
from app.tasks.portal_tasks import test_task, job_response, get_portal_task_id, submit_portal_task
from celery.result import AsyncResult

router = APIRouter(prefix="/celery", tags=["celery"])


@router.get("/test-celery")
def test_celery(background_tasks: BackgroundTasks, callback_url: str | None = None, portal_task_id: UUID | None = None):
    return submit_portal_task(test_task.s(), callback_url, portal_task_id and str(portal_task_id), background_tasks)


@router.get("/task-status/{task_id}")
def task_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    portal_task_id = get_portal_task_id(task_id)

    if result.failed():
        return job_response(task_id, result.status, str(result.result), portal_task_id)

    # status: PENDING, STARTED, SUCCESS, etc.; result stays None until the task finishes
    return job_response(task_id, result.status, result.result if result.ready() else None, portal_task_id)