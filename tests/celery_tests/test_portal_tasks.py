import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from celery.contrib.testing.worker import start_worker
from celery.result import AsyncResult
from celery import Signature
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from app.celery_app import celery_app
from app.main import app
from app.tasks.portal import check_data, common, smoke

client = TestClient(app)

CALLBACK = "https://portal/cb"
SECRET = "test-callback-secret"
SECRET_HEADERS = {"Content-Type": "application/json", "X-GDEX-Services-Secret": SECRET}


@pytest.fixture(scope="module", autouse=True)
def worker():
    """Run tasks on a real in-process worker rather than eagerly (conftest.py):
    eager mode re-raises chain failures at submit time and skips the errback path."""
    celery_app.conf.task_always_eager = False
    with start_worker(celery_app, perform_ping_check=False, pool="solo", loglevel="WARNING"):
        yield
    celery_app.conf.task_always_eager = True


@pytest.fixture
def callback_post(monkeypatch):
    monkeypatch.setenv("GDEX_SERVICES_CALLBACK_SECRET", SECRET)
    with patch("app.utils.callback.httpx.post") as post:
        yield post


@pytest.fixture(autouse=True)
def boreas():
    """Stand in for the Boreas S3 client; yields put_object to inspect uploaded logs."""
    client = MagicMock()
    with patch("app.utils.job_log.s3_client", return_value=client):
        yield client.put_object


def _log_url(job_id):
    return f"https://boreas.hpc.ucar.edu/gdex-services/services_tmp/logs/check_data.{job_id}.jsonl"


def _uploaded_log(put_object):
    return [json.loads(line) for line in put_object.call_args.kwargs["Body"].decode().splitlines()]


@pytest.fixture(autouse=True)
def reachable(monkeypatch):
    """pytest's tmp dirs sit under a 0700 directory, so treat their parents as traversable."""
    monkeypatch.setattr(check_data, "_others_can_reach", lambda path: True)


@pytest.fixture
def data_dir(tmp_path):
    tmp_path.chmod(0o755)
    for name, data in [("a.nc", b"abc"), ("b.nc", b"hello")]:
        (tmp_path / name).write_bytes(data)
        (tmp_path / name).chmod(0o644)
    return tmp_path


def _step(status, result=None, error=None, details=None):
    return {"status": status, "result": result, "error": error, "details": details}


def _readable_details(file_count):
    return {"file_count": file_count, "unreadable_file_count": 0, "unreadable_directories": [], "unscanned_directories": []}


URL_ERROR = "The provided endpoint is a URL, but this function only supports endpoint paths on /glade: https://example.com/x"


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
    def test_returns_job_id_before_running(self):
        body = client.get("/celery/test-celery").json()

        assert body == {"external_job_id": body["external_job_id"], "status": "SUBMITTED", "result": None}
        assert body["external_job_id"]

    def test_task_is_queued_only_after_the_response(self):
        background_tasks = BackgroundTasks()
        with patch.object(Signature, "apply_async") as apply_async:
            body = common.submit_portal_task(smoke.test_task.s(), background_tasks=background_tasks)

            assert body["status"] == "SUBMITTED"
            apply_async.assert_not_called()
            asyncio.run(background_tasks())  # what FastAPI does once the response is sent
            apply_async.assert_called_once()


class TestStatus:
    def test_success_reports_every_step(self, data_dir):
        job_id = client.get("/files/check-data", params={"path": str(data_dir)}).json()["external_job_id"]
        _wait(job_id)

        assert _status(job_id) == {
            "external_job_id": job_id,
            "status": "SUCCESS",
            "result": {
                "path": str(data_dir),
                "log_url": _log_url(job_id),
                "tasks": {
                    "validate_path": _step("SUCCESS", True, details={"is_directory": True}),
                    "check_readable": _step("SUCCESS", True, details=_readable_details(2)),
                    "data_size": _step("SUCCESS", 8),
                },
            },
        }

    def test_single_file_size(self, data_dir):
        path = str(data_dir / "a.nc")
        job_id = client.get("/files/check-data", params={"path": path}).json()["external_job_id"]
        _wait(job_id)

        tasks = _status(job_id)["result"]["tasks"]
        assert tasks["validate_path"]["details"] == {"is_directory": False}
        assert tasks["check_readable"] == _step("SUCCESS", True, details=_readable_details(1))
        assert tasks["data_size"] == _step("SUCCESS", 3)

    def test_url_path_fails_validation_and_skips_the_rest(self):
        job_id = client.get("/files/check-data", params={"path": "https://example.com/x"}).json()["external_job_id"]
        _wait(job_id)

        body = _status(job_id)
        assert body["status"] == "FAILURE"
        assert body["result"] == {
            "path": "https://example.com/x",
            "log_url": _log_url(job_id),
            "tasks": {
                "validate_path": _step("FAILURE", error=URL_ERROR),
                "check_readable": _step("SKIPPED"),
                "data_size": _step("SKIPPED"),
            },
        }

    def test_missing_path_fails_validation(self, tmp_path):
        missing = str(tmp_path / "nope")
        job_id = client.get("/files/check-data", params={"path": missing}).json()["external_job_id"]
        _wait(job_id)

        body = _status(job_id)
        assert body["status"] == "FAILURE"
        assert body["result"]["tasks"]["validate_path"] == _step("FAILURE", error=f"Path does not exist: {missing}")

    def test_step_error_is_recorded_as_failure(self, data_dir, monkeypatch):
        def boom(path):
            raise PermissionError("cannot stat parent")
        monkeypatch.setattr(check_data, "_others_can_reach", boom)
        job_id = client.get("/files/check-data", params={"path": str(data_dir)}).json()["external_job_id"]
        _wait(job_id)

        body = _status(job_id)
        assert body["status"] == "FAILURE"
        assert body["result"]["tasks"]["check_readable"] == _step("FAILURE", error="cannot stat parent")
        assert body["result"]["tasks"]["data_size"] == _step("SKIPPED")

    def test_unknown_task_is_pending(self):
        assert _status("does-not-exist") == {
            "external_job_id": "does-not-exist", "status": "PENDING", "result": None,
        }


class TestReadability:
    def _tasks(self, path):
        job_id = client.get("/files/check-data", params={"path": str(path)}).json()["external_job_id"]
        _wait(job_id)
        body = _status(job_id)
        assert body["status"] == "SUCCESS"  # unreadable files are a False result, not a failure
        return body["result"]["tasks"]

    def test_unreadable_files_grouped_by_directory_and_size_skipped(self, data_dir):
        sub = data_dir / "sub"
        sub.mkdir(mode=0o755)
        for name in ["c.nc", "d.nc"]:
            (sub / name).write_bytes(b"x")
            (sub / name).chmod(0o600)
        (data_dir / "a.nc").chmod(0o600)

        tasks = self._tasks(data_dir)

        assert tasks["check_readable"] == _step("SUCCESS", False, details={
            "file_count": 4,
            "unreadable_file_count": 3,
            "unreadable_directories": [
                {"directory": str(data_dir), "unreadable_files": 1},
                {"directory": str(sub), "unreadable_files": 2},
            ],
            "unscanned_directories": [],
        })
        assert tasks["data_size"] == _step("SKIPPED")

    def test_files_under_unlistable_directory_are_unreadable(self, data_dir):
        sub = data_dir / "locked"
        sub.mkdir()
        (sub / "c.nc").write_bytes(b"x")
        (sub / "c.nc").chmod(0o644)
        sub.chmod(0o750)  # worker can still walk it, other users can't

        tasks = self._tasks(data_dir)

        assert tasks["check_readable"]["details"]["unreadable_directories"] == [{"directory": str(sub), "unreadable_files": 1}]
        assert tasks["data_size"] == _step("SKIPPED")

    def test_untraversable_parents_make_everything_unreadable(self, data_dir, monkeypatch):
        monkeypatch.setattr(check_data, "_others_can_reach", lambda path: False)

        details = self._tasks(data_dir)["check_readable"]["details"]

        assert details["unreadable_directories"] == [{"directory": str(data_dir), "unreadable_files": 2}]
        assert details["unreadable_file_count"] == 2

    def test_unreadable_single_file(self, data_dir):
        (data_dir / "a.nc").chmod(0o600)

        tasks = self._tasks(data_dir / "a.nc")

        assert tasks["check_readable"]["result"] is False
        assert tasks["check_readable"]["details"]["unreadable_directories"] == [{"directory": str(data_dir), "unreadable_files": 1}]
        assert tasks["data_size"] == _step("SKIPPED")


class TestJobLog:
    def _run(self, path):
        job_id = client.get("/files/check-data", params={"path": str(path)}).json()["external_job_id"]
        _wait(job_id)
        return job_id, _status(job_id)["result"]

    def test_log_is_uploaded_once_under_the_job_id(self, data_dir, boreas):
        job_id, result = self._run(data_dir)

        boreas.assert_called_once()
        kwargs = boreas.call_args.kwargs
        assert kwargs["Bucket"] == "gdex-services"
        assert kwargs["Key"] == f"services_tmp/logs/check_data.{job_id}.jsonl"
        assert kwargs["ACL"] == "public-read"
        assert result["log_url"] == _log_url(job_id)
        assert "_log" not in result

    def test_log_lines_use_the_gdexws_format(self, data_dir, boreas):
        self._run(data_dir)

        lines = _uploaded_log(boreas)
        assert [(l["command"], l["level"], l["process_message"]) for l in lines] == [
            ("check-data", "INFO", "check-data job started"),
            ("validate_path", "INFO", "validate_path started"),
            ("validate_path", "INFO", "validate_path completed"),
            ("check_readable", "INFO", "check_readable started"),
            ("check_readable", "INFO", "check_readable completed"),
            ("data_size", "INFO", "data_size started"),
            ("data_size", "INFO", "data_size completed"),
            ("check-data", "INFO", "check-data job completed"),
        ]
        assert all(set(l) >= {"command", "time_of_process", "level", "process_message"} for l in lines)
        assert lines[0]["path"] == str(data_dir)
        assert lines[6]["result"] == 8

    def test_unreadable_and_failed_steps_are_logged(self, data_dir, boreas, tmp_path):
        (data_dir / "a.nc").chmod(0o600)
        self._run(data_dir)
        levels = [(l["process_message"], l["level"]) for l in _uploaded_log(boreas)]
        assert ("check_readable completed with result False", "WARNING") in levels
        assert ("data_size skipped", "INFO") in levels
        assert levels[-1] == ("check-data job incomplete", "WARNING")

        self._run(tmp_path / "nope")
        lines = _uploaded_log(boreas)
        assert ("validate_path failed", "ERROR") in [(l["process_message"], l["level"]) for l in lines]
        assert lines[-1]["process_message"] == "check-data job failed"
        assert lines[-1]["level"] == "ERROR"

    def test_failed_upload_does_not_fail_the_job(self, data_dir, boreas):
        boreas.side_effect = RuntimeError("boreas down")

        job_id, result = self._run(data_dir)

        assert _status(job_id)["status"] == "SUCCESS"
        assert result["log_url"] is None
        assert result["tasks"]["data_size"]["result"] == 8


class TestCallbacks:
    def test_success_posts_job_id(self, callback_post):
        job_id = client.get("/celery/test-celery", params={"callback_url": CALLBACK}).json()["external_job_id"]
        _wait_for_callback(callback_post)

        callback_post.assert_called_once_with(CALLBACK, json={
            "external_job_id": job_id, "status": "SUCCESS", "result": "Yo, Celery is working!",
        }, headers=SECRET_HEADERS, timeout=10)

    def test_chain_success_posts_once(self, callback_post, data_dir):
        job_id = client.get("/files/check-data", params={"path": str(data_dir), "callback_url": CALLBACK}).json()["external_job_id"]
        _wait_for_callback(callback_post)

        callback_post.assert_called_once()
        payload = callback_post.call_args.kwargs["json"]
        assert payload["external_job_id"] == job_id
        assert payload["status"] == "SUCCESS"
        assert payload["result"]["tasks"]["data_size"]["result"] == 8

    def test_failure_posts_once_with_standard_result(self, callback_post):
        job_id = client.get("/files/check-data", params={"path": "https://example.com/x", "callback_url": CALLBACK}).json()["external_job_id"]
        _wait_for_callback(callback_post)

        callback_post.assert_called_once_with(CALLBACK, json={
            "external_job_id": job_id,
            "status": "FAILURE",
            "result": {
                "path": "https://example.com/x",
                "log_url": _log_url(job_id),
                "tasks": {
                    "validate_path": _step("FAILURE", error=URL_ERROR),
                    "check_readable": _step("SKIPPED"),
                    "data_size": _step("SKIPPED"),
                },
            },
        }, headers=SECRET_HEADERS, timeout=10)

    def test_no_callback_url_posts_nothing(self, callback_post, data_dir):
        job_id = client.get("/files/check-data", params={"path": str(data_dir)}).json()["external_job_id"]
        _wait(job_id)
        time.sleep(0.3)

        callback_post.assert_not_called()
