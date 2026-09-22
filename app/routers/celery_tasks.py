from fastapi import APIRouter
from app.tasks import test_task
from celery.result import AsyncResult

router = APIRouter(prefix="/celery", tags=["celery"])

@router.get("/test-celery")
def test_celery():
    task = test_task.delay()

    return {
        "task_id": task.id,
        "status": "submitted"
    }

@router.get("/task-status/{task_id}")
def task_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "status": result.status,   # PENDING, STARTED, SUCCESS, FAILURE, etc.
        "result": result.result if result.ready() else None,
    }
