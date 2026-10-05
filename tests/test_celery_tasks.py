import json
from unittest.mock import MagicMock, patch

import pytest
from celery.result import AsyncResult

from app.celery_app import celery_app
from app.tasks import transform_tasks
from gdexws.utils.logging import log_format

REQUEST_DICT = {
    "Files": ["Web-services/test.nc"],
    "Commands": [{"command": "add_global_meta", "global-attr-name": "gdex_dsid", "global-attr-value": "d99ext9"}],
    "CallbackUrl": None,
}


@pytest.fixture
def workdir(tmp_path):
    """Point the tasks' WORKDIR (normally /glade/...) at tmp_path."""
    with patch.object(transform_tasks, "WORKDIR", str(tmp_path)):
        yield tmp_path


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.pgadd = MagicMock(return_value=1234)
    with patch.object(transform_tasks, "PgDBI", return_value=db):
        yield db


def _write_jsonl(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _pbs_line(message, level="INFO", **kwargs):
    return {"command": "pbs", "time_of_process": "2026-09-30T12:00:00Z",
            "level": level, "process_message": message, **kwargs}


class TestMapJsonlToCeleryState:
    def test_info_line_is_progress(self):
        entry = log_format("transform", "INFO", "Processing file 1 of 2")
        assert transform_tasks.jsonl_to_celery_state(entry) == "PROGRESS"

    def test_debug_and_warning_lines_are_progress(self):
        for level in ("DEBUG", "WARNING"):
            entry = log_format("add_global_meta", level, "msg")
            assert transform_tasks.jsonl_to_celery_state(entry) == "PROGRESS"

    def test_error_line_is_failure_reported(self):
        entry = log_format("add_global_meta", "ERROR", "File not found", file="x.nc")
        assert transform_tasks.jsonl_to_celery_state(entry) == "PBS_FAILURE_REPORTED"


class TestBuildAndUploadArtifacts:
    def test_returns_urls(self):
        with patch.object(transform_tasks, "create_transform_payload", return_value="https://b/payload.json") as payload, \
             patch.object(transform_tasks, "create_pbs_script", return_value="https://b/x.pbs") as pbs:
            result = transform_tasks.create_boreas_artifacts.apply(args=[REQUEST_DICT, "rid"]).get()

        assert result == {"request_id": "rid", "payload_url": "https://b/payload.json", "pbs_url": "https://b/x.pbs"}
        request = payload.call_args.args[0]
        assert request.files == ["Web-services/test.nc"]
        assert request.commands[0].model_dump()["global-attr-name"] == "gdex_dsid"
        pbs.assert_called_once_with("https://b/payload.json", request_id="rid")


class TestSubmitDscheckDownload:
    def test_inserts_curl_row(self, mock_db):
        artifacts = {"request_id": "rid", "payload_url": "p", "pbs_url": "https://b/x.pbs"}
        result = transform_tasks.add_dscheck_download.apply(args=[artifacts, "chiaweih"]).get()

        assert result["cindex_download"] == 1234
        assert result["specialist"] == "chiaweih"
        row = mock_db.pgadd.call_args.args[1]
        assert row["command"] == "curl"
        assert row["argv"] == '-o transform.rid.pbs "$DOWNLOAD_URL"'
        assert row["environments"] == "DOWNLOAD_URL=https://b/x.pbs"

    def test_never_exits_worker_process(self, mock_db):
        artifacts = {"request_id": "rid", "payload_url": "p", "pbs_url": "u"}
        transform_tasks.add_dscheck_download.apply(args=[artifacts, "chiaweih"]).get()
        logact = mock_db.pgadd.call_args.args[2]
        assert not logact & transform_tasks.PgLOG.EXITLG

    def test_duplicate_row_reuses_existing_cindex(self, mock_db):
        # Redelivery: dscheck rejects the (command, argv) duplicate, the row from the first run is found
        mock_db.pgadd.return_value = 0
        mock_db.pgget.return_value = {"cindex": 999}
        artifacts = {"request_id": "rid", "payload_url": "p", "pbs_url": "u"}
        result = transform_tasks.add_dscheck_download.apply(args=[artifacts, "chiaweih"]).get()

        assert result["cindex_download"] == 999
        mock_db.pgadd.assert_called_once()
        condition = mock_db.pgget.call_args.args[2]
        assert condition == """command = 'curl' AND argv = '-o transform.rid.pbs "$DOWNLOAD_URL"'"""

    def test_invalid_cindex_retries_then_fails(self, mock_db):
        mock_db.pgadd.return_value = 0
        mock_db.pgget.return_value = {}
        artifacts = {"request_id": "rid", "payload_url": "p", "pbs_url": "u"}
        result = transform_tasks.add_dscheck_download.apply(args=[artifacts, "chiaweih"])
        assert result.state == "FAILURE"
        # 1 initial attempt + max_retries
        assert mock_db.pgadd.call_count == 1 + transform_tasks.add_dscheck_download.max_retries


class TestWaitForScriptAndSubmitQsub:
    def test_submits_qsub_when_script_exists(self, workdir, mock_db):
        (workdir / "transform.rid.pbs").write_text("#!/bin/bash")
        prev = {"request_id": "rid", "specialist": "chiaweih"}
        result = transform_tasks.add_dscheck_qsub.apply(args=[prev]).get()

        assert result["cindex_submit"] == 1234
        row = mock_db.pgadd.call_args.args[1]
        assert row["command"] == "qsub"
        assert row["argv"] == "-v REQUEST_ID transform.rid.pbs"
        assert row["environments"] == "REQUEST_ID=rid"
        assert row["workdir"] == str(workdir)

    def test_duplicate_qsub_row_reuses_existing_cindex(self, workdir, mock_db):
        (workdir / "transform.rid.pbs").write_text("#!/bin/bash")
        mock_db.pgadd.return_value = 0
        mock_db.pgget.return_value = {"cindex": 888}
        prev = {"request_id": "rid", "specialist": "chiaweih"}
        result = transform_tasks.add_dscheck_qsub.apply(args=[prev]).get()

        assert result["cindex_submit"] == 888
        assert mock_db.pgget.call_args.args[2] == "command = 'qsub' AND argv = '-v REQUEST_ID transform.rid.pbs'"

    def test_missing_script_retries_without_submitting(self, workdir, mock_db):
        prev = {"request_id": "rid", "specialist": "chiaweih"}
        result = transform_tasks.add_dscheck_qsub.apply(args=[prev])
        assert result.state == "FAILURE"
        mock_db.pgadd.assert_not_called()


class TestWatchJob:
    @pytest.mark.parametrize("message,level,job_status", [
        ("PBS job completed", "INFO", "completed"),
        ("PBS job failed", "ERROR", "failed"),
    ])
    def test_terminal_line_succeeds_with_job_status(self, workdir, message, level, job_status):
        _write_jsonl(workdir / "rid-1.gdexws.jsonl", [_pbs_line("PBS job started"), _pbs_line(message, level)])
        with patch.object(transform_tasks.notify_callback, "delay") as notify:
            result = transform_tasks.watch_job.apply(args=["rid-1"], task_id="rid-1")

        # A failed PBS job is still Celery SUCCESS — the outcome is in the result
        assert result.state == "SUCCESS"
        assert result.result["job_status"] == job_status
        assert result.result["request_id"] == "rid-1"
        assert result.result["process_message"] == message
        notify.assert_not_called()

    def test_terminal_line_fires_callback_when_provided(self, workdir):
        _write_jsonl(workdir / "rid-2.gdexws.jsonl", [_pbs_line("PBS job completed")])
        with patch.object(transform_tasks.notify_callback, "delay") as notify:
            transform_tasks.watch_job.apply(args=["rid-2", "https://portal/cb"], task_id="rid-2")

        notify.assert_called_once()
        payload, url = notify.call_args.args
        assert url == "https://portal/cb"
        assert payload["job_status"] == "completed"

    def test_in_progress_mirrors_latest_line_and_reschedules(self, workdir):
        entries = [_pbs_line("PBS job started"), log_format("add_global_meta", "INFO", "Added attribute")]
        _write_jsonl(workdir / "rid-3.gdexws.jsonl", entries)
        with patch.object(transform_tasks.watch_job, "apply_async") as reschedule:
            transform_tasks.watch_job.apply(args=["rid-3", "https://portal/cb"], task_id="rid-3")

        status = AsyncResult("rid-3", app=celery_app)
        assert status.state == "PROGRESS"
        assert status.info["process_message"] == "Added attribute"
        reschedule.assert_called_once()
        kwargs = reschedule.call_args.kwargs
        assert kwargs["args"] == ["rid-3", "https://portal/cb", 2]
        assert kwargs["task_id"] == "rid-3"

    def test_error_line_reports_failure_state(self, workdir):
        _write_jsonl(workdir / "rid-4.gdexws.jsonl", [log_format("add_global_meta", "ERROR", "bad file")])
        with patch.object(transform_tasks.watch_job, "apply_async"):
            transform_tasks.watch_job.apply(args=["rid-4"], task_id="rid-4")

        assert AsyncResult("rid-4", app=celery_app).state == "PBS_FAILURE_REPORTED"

    def test_resumes_from_last_line(self, workdir):
        _write_jsonl(workdir / "rid-5.gdexws.jsonl", [_pbs_line("PBS job completed"), _pbs_line("PBS job started")])
        with patch.object(transform_tasks.watch_job, "apply_async") as reschedule:
            transform_tasks.watch_job.apply(args=["rid-5", None, 1], task_id="rid-5")

        # Line 0 (terminal) was already consumed in a previous poll, so not re-reported
        assert AsyncResult("rid-5", app=celery_app).state == "PROGRESS"
        assert reschedule.call_args.kwargs["args"] == ["rid-5", None, 2]

    def test_partial_trailing_line_is_left_for_next_poll(self, workdir):
        (workdir / "rid-6.gdexws.jsonl").write_text(json.dumps(_pbs_line("PBS job started")) + '\n{"command": "tra')
        with patch.object(transform_tasks.watch_job, "apply_async") as reschedule:
            transform_tasks.watch_job.apply(args=["rid-6"], task_id="rid-6")

        assert AsyncResult("rid-6", app=celery_app).state == "PROGRESS"
        assert reschedule.call_args.kwargs["args"] == ["rid-6", None, 1]

    def test_missing_log_reschedules_and_stays_pending(self, workdir):
        with patch.object(transform_tasks.watch_job, "apply_async") as reschedule:
            transform_tasks.watch_job.apply(args=["rid-7"], task_id="rid-7")

        assert AsyncResult("rid-7", app=celery_app).state == "PENDING"
        assert reschedule.call_args.kwargs["args"] == ["rid-7", None, 0]
        assert reschedule.call_args.kwargs["countdown"] == transform_tasks.SEARCH_JSONL_INT

    def test_bad_json_is_watcher_failure(self, workdir):
        (workdir / "rid-8.gdexws.jsonl").write_text("not json\n")
        result = transform_tasks.watch_job.apply(args=["rid-8"], task_id="rid-8")
        assert result.state == "FAILURE"


class TestNotifyCallback:
    def test_posts_result(self):
        with patch.object(transform_tasks.httpx, "post") as post:
            post.return_value = MagicMock(raise_for_status=MagicMock())
            transform_tasks.notify_callback.apply(args=[{"job_status": "completed"}, "https://portal/cb"]).get()

        post.assert_called_once_with("https://portal/cb", json={"job_status": "completed"}, timeout=10)

    def test_retries_on_http_error(self):
        with patch.object(transform_tasks.httpx, "post", side_effect=transform_tasks.httpx.ConnectError("down")) as post:
            result = transform_tasks.notify_callback.apply(args=[{}, "https://portal/cb"])

        assert result.state == "FAILURE"
        assert post.call_count == 1 + transform_tasks.notify_callback.max_retries


class TestStartTransformPipeline:
    def test_watch_job_uses_request_id_and_callback(self):
        with patch.object(transform_tasks, "chain") as chain, \
             patch.object(transform_tasks.watch_job, "apply_async") as watch:
            transform_tasks.celery_transform({**REQUEST_DICT, "CallbackUrl": "https://portal/cb"}, "rid", "chiaweih")

        chain.return_value.apply_async.assert_called_once()
        watch.assert_called_once_with(args=["rid", "https://portal/cb"], task_id="rid")
