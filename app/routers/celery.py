from fastapi import APIRouter, BackgroundTasks
from app.celery_app import celery_app
from app.tasks.portal.common import job_response, job_status, submit_portal_task
from app.tasks.portal.smoke import test_task
from celery.result import AsyncResult

router = APIRouter(prefix="/celery", tags=["celery"])


@router.get("/test-celery")
def test_celery(background_tasks: BackgroundTasks, callback_url: str | None = None):
    return submit_portal_task(test_task.s(), callback_url, background_tasks)


@router.get("/task-status/{task_id}")
def task_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)

    if result.failed():
        return job_response(task_id, result.status, str(result.result))

    # status: PENDING, STARTED, SUCCESS, etc.; result stays None until the task finishes
    value = result.result if result.ready() else None
    return job_response(task_id, job_status(result.status, value), value)