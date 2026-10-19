import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import compose
from app.tasks import transform_tasks

client = TestClient(app)

PAYLOAD = {
    "Files": ["Web-services/test.nc"],
    "Commands": [{"command": "add_global_meta", "global-attr-name": "gdex_dsid", "global-attr-value": "d99ext9"}],
}


@pytest.fixture
def mock_pipeline(tmp_path):
    """Run the real pipeline eagerly with Boreas/dscheck mocked and WORKDIR
    on tmp_path. The request_id is fixed so the downloaded PBS script and a
    finished JSONL log exist up front: the watcher is published before the
    chain, so it must find the log on its first (eager) poll."""
    db = MagicMock()
    db.pgadd = MagicMock(side_effect=[111, 222])

    request_id = uuid.uuid4()
    (tmp_path / f"transform.{request_id}.pbs").write_text("#!/bin/bash")
    (tmp_path / f"{request_id}.gdexws.jsonl").write_text(json.dumps(
        {"command": "pbs", "time_of_process": "t", "level": "INFO", "process_message": "PBS job completed"}
    ) + "\n")

    def fake_pbs(payload_url, request_id):
        return f"https://b/transform.{request_id}.pbs"

    with patch.object(compose.uuid, "uuid4", return_value=request_id), \
         patch.object(transform_tasks, "WORKDIR", str(tmp_path)), \
         patch.object(transform_tasks, "PgDBI", return_value=db), \
         patch.object(transform_tasks, "create_transform_payload", return_value="https://b/payload.json"), \
         patch.object(transform_tasks, "create_pbs_script", side_effect=fake_pbs), \
         patch.object(transform_tasks.notify_callback, "apply_async") as notify:
        yield {"db": db, "notify": notify}


class TestPostTransform:
    def test_response_shape(self, mock_pipeline):
        resp = client.post("/compose/transform?issuer=user@ucar.edu", json=PAYLOAD)

        assert resp.status_code == 200
        body = resp.json()
        rid = body["request_id"]
        assert body["issuer"] == "user@ucar.edu"
        assert body["status_url"] == f"/compose/status/{rid}"
        assert body["log_url"] == f"/compose/log/{rid}"

        commands = [c.args[1]["command"] for c in mock_pipeline["db"].pgadd.call_args_list]
        assert commands == ["curl", "qsub"]
        mock_pipeline["notify"].assert_not_called()

    def test_status_reflects_finished_job(self, mock_pipeline):
        rid = client.post("/compose/transform", json=PAYLOAD).json()["request_id"]

        resp = client.get(f"/compose/status/{rid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["external_job_id"] == rid
        assert body["job_type"] == "transform"
        assert body["status"] == "SUCCESS"
        assert "Completed successfully" in body["result"]["msg"]
        assert body["steps"]["artifacts"] == "SUCCESS"
        assert body["steps"]["download"] == "SUCCESS"
        assert body["steps"]["qsub"] == "SUCCESS"

    def test_callbacks_sent_with_tracked_task_ids(self, mock_pipeline):
        url = compose.ALLOWED_CALLBACK_URLs[0]
        resp = client.post("/compose/transform", json={**PAYLOAD, "CallbackUrl": url})
        assert resp.status_code == 200
        rid = resp.json()["request_id"]

        calls = mock_pipeline["notify"].call_args_list
        # start callback from the API, then the final one from the (eager) watcher
        assert [c.kwargs["task_id"] for c in calls] == [f"{rid}:callback-start", f"{rid}:callback-final"]
        assert calls[0].kwargs["args"][1] == url
        assert calls[0].kwargs["args"][0]["status"] == "PENDING"
        assert calls[1].kwargs["args"][0]["status"] == "SUCCESS"

    def test_callback_url_not_allowed_is_rejected(self, mock_pipeline):
        resp = client.post("/compose/transform", json={**PAYLOAD, "CallbackUrl": "https://evil.example/cb"})
        assert resp.status_code == 403
        mock_pipeline["notify"].assert_not_called()
        mock_pipeline["db"].pgadd.assert_not_called()

    def test_escaping_file_path_is_rejected_before_queueing(self, mock_pipeline):
        err = {"status_message": "Access denied: 'x' escapes base directory"}
        with patch.object(compose, "relpath_validate", return_value=err), \
             patch.object(compose, "celery_transform") as transform:
            resp = client.post("/compose/transform", json={**PAYLOAD, "CallbackUrl": compose.ALLOWED_CALLBACK_URLs[0]})
        assert resp.status_code == 403
        assert resp.json() == err
        transform.assert_not_called()
        mock_pipeline["notify"].assert_not_called()

    def test_broker_down_returns_503(self):
        with patch.object(compose, "celery_transform", side_effect=ConnectionError("redis down")):
            resp = client.post("/compose/transform", json=PAYLOAD)
        assert resp.status_code == 503


class TestStatusRouting:
    def test_unknown_request_id_is_pending(self):
        resp = client.get("/compose/status/550e8400-e29b-41d4-a716-446655440000")
        assert resp.status_code == 200
        assert resp.json()["status"] == "PENDING"

    def test_status_lists_every_step(self):
        rid = "550e8400-e29b-41d4-a716-446655440000"
        steps = client.get(f"/compose/status/{rid}").json()["steps"]
        assert list(steps) == transform_tasks.STEP_NAMES

    def test_integer_path_still_hits_dscheck_endpoint(self):
        with patch.object(compose, "get_dscheck_json", return_value={"cindex": 4071816}) as dscheck:
            resp = client.get("/compose/status/4071816")
        assert resp.status_code == 200
        dscheck.assert_called_once_with(4071816)
