import json
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
    on tmp_path; pre-create the downloaded PBS script and a finished JSONL
    log so the chain and watch_job both complete in one pass."""
    db = MagicMock()
    db.pgadd = MagicMock(side_effect=[111, 222])

    def fake_pbs(payload_url, request_id):
        (tmp_path / f"transform.{request_id}.pbs").write_text("#!/bin/bash")
        (tmp_path / f"{request_id}.gdexws.jsonl").write_text(json.dumps(
            {"command": "pbs", "time_of_process": "t", "level": "INFO", "process_message": "PBS job completed"}
        ) + "\n")
        return f"https://b/transform.{request_id}.pbs"

    with patch.object(transform_tasks, "WORKDIR", str(tmp_path)), \
         patch.object(transform_tasks, "PgDBI", return_value=db), \
         patch.object(transform_tasks, "create_transform_payload", return_value="https://b/payload.json"), \
         patch.object(transform_tasks, "create_pbs_script", side_effect=fake_pbs), \
         patch.object(transform_tasks.notify_callback, "delay") as notify:
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
        assert body["request_id"] == rid
        assert body["state"] == "SUCCESS"
        assert body["info"]["job_status"] == "completed"

    def test_callback_url_reaches_watcher(self, mock_pipeline):
        resp = client.post("/compose/transform", json={**PAYLOAD, "CallbackUrl": "https://portal/cb"})
        assert resp.status_code == 200
        mock_pipeline["notify"].assert_called_once()
        assert mock_pipeline["notify"].call_args.args[1] == "https://portal/cb"

    def test_broker_down_returns_503(self):
        with patch.object(compose, "celery_transform", side_effect=ConnectionError("redis down")):
            resp = client.post("/compose/transform", json=PAYLOAD)
        assert resp.status_code == 503


class TestStatusRouting:
    def test_unknown_request_id_is_pending(self):
        resp = client.get("/compose/status/550e8400-e29b-41d4-a716-446655440000")
        assert resp.status_code == 200
        assert resp.json()["state"] == "PENDING"

    def test_integer_path_still_hits_dscheck_endpoint(self):
        with patch.object(compose, "get_dscheck_json", return_value={"cindex": 4071816}) as dscheck:
            resp = client.get("/compose/status/4071816")
        assert resp.status_code == 200
        dscheck.assert_called_once_with(4071816)
