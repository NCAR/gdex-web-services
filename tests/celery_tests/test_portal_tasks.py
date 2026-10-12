import time
from unittest.mock import patch

import pytest
from celery.contrib.testing.worker import start_worker
from celery.result import AsyncResult
from fastapi.testclient import TestClient

from app.celery_app import celery_app
from app.main import app

client = TestClient(app)

PORTAL_ID = "3f2b8c1e-9d4a-4e6b-8a7c-1d2e3f4a5b6c"
CALLBACK = "https://portal/cb"


@pytest.fixture(scope="module", autouse=True)
def worker():
    """Run tasks on a real in-process worker rather than eagerly (conftest.py):
    eager mode re-raises chain failures at submit time and skips the errback path."""
    celery_app.conf.task_always_eager = False
    with start_worker(celery_app, perform_ping_check=False, pool="solo", loglevel="WARNING"):
        yield
    celery_app.conf.task_always_eager = True


@pytest.fixture
def callback_post():
    with patch("app.tasks.portal_tasks.requests.post") as post:
        yield post


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "a.nc").write_bytes(b"abc")
    (tmp_path / "b.nc").write_bytes(b"hello")
    return tmp_path


def _wait(task_id):
    AsyncResult(task_id, app=celery_app).get(timeout=10, propagate=False)


def _wait_for_callback(post):
    deadline = time.time() + 5
    while not post.called and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)  # let any duplicate callback land before callers assert call_count


def _status(task_id):
    return client.get(f"/celery/task-status/{task_id}").json()


class TestSubmit:
    def test_returns_celery_and_portal_ids(self):
        body = client.get("/celery/test-celery", params={"portal_task_id": PORTAL_ID}).json()

        assert body["portal_task_id"] == PORTAL_ID
        assert body["external_job_id"] and body["external_job_id"] != PORTAL_ID
        assert body["status"] == "SUBMITTED"
        assert body["result"] is None

    def test_portal_id_is_optional(self, data_dir):
        body = client.get("/files/get-data-size", params={"path": str(data_dir)}).json()
        _wait(body["external_job_id"])

        assert body["portal_task_id"] is None
        assert _status(body["external_job_id"])["portal_task_id"] is None

    def test_invalid_portal_id_is_rejected(self):
        resp = client.get("/celery/test-celery", params={"portal_task_id": "not-a-uuid"})
        assert resp.status_code == 422


class TestStatus:
    def test_success_returns_result_and_portal_id(self, data_dir):
        job_id = client.get("/files/get-data-size", params={"path": str(data_dir), "portal_task_id": PORTAL_ID}).json()["external_job_id"]
        _wait(job_id)

        assert _status(job_id) == {
            "external_job_id": job_id,
            "portal_task_id": PORTAL_ID,
            "status": "SUCCESS",
            "result": {"path": str(data_dir), "is_directory": True, "size_bytes": 8},
        }

    def test_single_file_size(self, data_dir):
        path = str(data_dir / "a.nc")
        job_id = client.get("/files/get-data-size", params={"path": path}).json()["external_job_id"]
        _wait(job_id)

        assert _status(job_id)["result"] == {"path": path, "is_directory": False, "size_bytes": 3}

    def test_url_path_fails_with_message(self):
        job_id = client.get("/files/get-data-size", params={"path": "https://example.com/x", "portal_task_id": PORTAL_ID}).json()["external_job_id"]
        _wait(job_id)

        body = _status(job_id)
        assert body["status"] == "FAILURE"
        assert body["portal_task_id"] == PORTAL_ID
        assert body["result"] == "The provided endpoint is a URL, but this function only supports endpoint paths on /glade: https://example.com/x"

    def test_missing_path_fails_with_message(self, tmp_path):
        missing = str(tmp_path / "nope")
        job_id = client.get("/files/get-data-size", params={"path": missing}).json()["external_job_id"]
        _wait(job_id)

        body = _status(job_id)
        assert body["status"] == "FAILURE"
        assert body["result"] == f"Path does not exist: {missing}"

    def test_unknown_task_is_pending(self):
        assert _status("does-not-exist") == {
            "external_job_id": "does-not-exist", "portal_task_id": None, "status": "PENDING", "result": None,
        }


class TestCallbacks:
    def test_success_posts_both_ids(self, callback_post):
        job_id = client.get("/celery/test-celery", params={"callback_url": CALLBACK, "portal_task_id": PORTAL_ID}).json()["external_job_id"]
        _wait_for_callback(callback_post)

        callback_post.assert_called_once_with(CALLBACK, json={
            "external_job_id": job_id, "portal_task_id": PORTAL_ID, "status": "SUCCESS", "result": "Yo, Celery is working!",
        })

    def test_chain_success_posts_once(self, callback_post, data_dir):
        job_id = client.get("/files/get-data-size", params={"path": str(data_dir), "callback_url": CALLBACK, "portal_task_id": PORTAL_ID}).json()["external_job_id"]
        _wait_for_callback(callback_post)

        callback_post.assert_called_once()
        payload = callback_post.call_args.kwargs["json"]
        assert payload["external_job_id"] == job_id
        assert payload["status"] == "SUCCESS"
        assert payload["result"]["size_bytes"] == 8

    def test_failure_posts_once_with_error_message(self, callback_post):
        job_id = client.get("/files/get-data-size", params={"path": "https://example.com/x", "callback_url": CALLBACK, "portal_task_id": PORTAL_ID}).json()["external_job_id"]
        _wait_for_callback(callback_post)

        callback_post.assert_called_once_with(CALLBACK, json={
            "external_job_id": job_id,
            "portal_task_id": PORTAL_ID,
            "status": "FAILURE",
            "result": "The provided endpoint is a URL, but this function only supports endpoint paths on /glade: https://example.com/x",
        })

    def test_no_callback_url_posts_nothing(self, callback_post, data_dir):
        job_id = client.get("/files/get-data-size", params={"path": str(data_dir)}).json()["external_job_id"]
        _wait(job_id)
        time.sleep(0.3)

        callback_post.assert_not_called()
