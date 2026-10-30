"""
The router module for compose endpoints in the gdex-web-services FastAPI application.

This module defines endpoints for compose action (i.e. transform ... etc), and including status retrieval.

"""
from typing import Dict, Any
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from celery.result import AsyncResult
import uuid
import json
import logging

from app.schemas.models import TransformRequest
from app.celery_app import celery_app
from app.tasks.callback_task import CallbackError
from app.tasks.portal.common import job_response
from app.tasks.transform_tasks import celery_transform, STEP_NAMES
from app.utils import get_dscheck_json, relpath_validate
from app.utils.callback import ALLOWED_CALLBACK_URLS

# Import RDA/GDEX libraries (rda-python-common) for database interaction and logging.
try:
    from rda_python_common.pg_dbi import PgDBI
    from rda_python_common.pg_log import PgLOG
    RDA_AVAILABLE = True
except ImportError as e:
    raise RuntimeError(
        f"Failed to import rda_python_common: {e}\n"
        "Install with: pip install rda-python-common"
    ) from e


router = APIRouter(prefix="/compose", tags=["compose"])

logger = logging.getLogger(__name__)

# set up global constants
WORKDIR = str(Path('/glade/campaign/collections/gdex/data/exchange/Web-services/'))




# `{cindex:int}` only matches all-digit paths, so a UUID falls through to
# GET /status/{external_job_id} below instead of failing int validation here.
@router.get("/status/{cindex:int}")
async def get_dscheck_status(cindex: int) -> Dict[str, Any]:
    """
    Retrieve dscheck status and latest processing output for a specific cindex.

    Queue-level debugging tool. Clients should poll
    GET /compose/status/{external_job_id} instead.

    Queries the dscheck database record for the given cindex. Returns
    standardized JSON with the record's current status. For the job's own
    processing log, see GET /compose/log/{external_job_id}.

    Parameters
    ----------
    cindex : int
        The dscheck record index to query.
    issuer : str, optional
        Email or identifier of the person who initiated the request.

    Returns
    -------
    dict
        Standardized dscheck JSON containing:
        - cindex: Record index
        - time_of_status: ISO format timestamp
        - command: Command from record
        - argv: Arguments from record
        - specialist: Specialist assigned to record
        - issuer: Issuer identifier (if provided)
        - status_message: Human-readable status message
        - error: Error message (if applicable)

    Examples
    --------
    >>> curl -X GET https://api_url/compose/status/4071816
    >>> curl -X GET "https://api_url/compose/status/4071816?issuer=user@ucar.edu"
    """
    return get_dscheck_json(cindex)


@router.get("/status/{external_job_id}")
def get_celery_status(external_job_id: str) -> Dict[str, Any]:
    """
    Retrieve Redis-backed job status for a transformation job.

    Cheap enough to poll every few seconds. For the full step-by-step history,
    use GET /compose/log/{external_job_id}; for raw dscheck-row inspection, use
    GET /compose/status/{cindex}.

    Parameters
    ----------
    external_job_id : str
        The unique request identifier (UUID) returned by POST /compose/transform.

    Returns
    -------
    dict
        - job_type: Always "transform"
        - external_job_id: Request identifier
        - status: State of the watch_job task (task_id == external_job_id):
            - PENDING: log not written yet, or unknown/expired external_job_id
            - PBS_PROGRESS
            - PBS_FAILURE_REPORTED: the job logged an ERROR line
            - SUCCESS: the PBS job reached a terminal line; see result.status =
              completed|failed, so a failed PBS job is still SUCCESS here
            - FAILURE: the status watcher itself broke
        - result: Depends on status; whatever the status, it also holds `steps`
          and `callback`.
            - PBS_PROGRESS / PBS_FAILURE_REPORTED: the latest JSONL log entry
              (gdexws log_format dict)
            - SUCCESS: the final payload {job_type, external_job_id,
              status (completed|failed), result (terminal JSONL line), completed}
            - PENDING: {"msg": "log file not written yet"}, or {"msg": null} for
              an unknown/expired external_job_id
            - FAILURE: {"msg": error string}
            - steps: Celery state of each tracked side task (STEP_NAMES), run as
              task_id "{external_job_id}:{step}". PENDING means not started, not
              requested (no callback_url) or expired.
            - callback: {requested, status, info} for the portal callback_url.
                - requested: false when no callback_url was given (or the record expired)
                - status: NOT_REQUESTED, WAITING (job not finished yet), RETRY (portal
                  POST failed with a retryable error, backing off), SUCCESS (delivered)
                  or FAILURE (rejected with a 4xx, or retries exhausted)
                - info: {"http_status": 2xx} on SUCCESS; {"msg", "http_status"} on
                  RETRY / FAILURE, where http_status is the portal's status code (null
                  if it was never reached); null otherwise.
                  A 4xx other than 408/429 fails at once with no retry.
        - time: UTC ISO timestamp of this response

    Example:

    ```json
    {
        "external_job_id": "886c24a2-b67f-4a7d-94b0-2336dfade9bc",
        "status": "PENDING",
        "result": {
            "msg": "log file not written yet",
            "steps": {
                "artifacts": "SUCCESS",
                "download": "SUCCESS",
                "qsub": "RETRY",
                "callback-final": "WAITING"
            },
            "callback": {
                "requested": true,
                "status": "WAITING",
                "info": null
            }
        },
        "job_type": "transform",
        "time": "2026-10-28T17:15:36.455343+00:00"
    }
    ```

    Examples
    --------
    >>> curl -X GET https://api_url/compose/status/550e8400-e29b-41d4-a716-446655440000
    """
    worker_result = AsyncResult(external_job_id, app=celery_app)
    meta = worker_result.info
    # parse the latest log entry if it exists
    if isinstance(meta, dict):
        pass
    else:
        # FAILURE gives an Exception instance,
        # An unknown external_job_id gives None
        meta = {"msg": str(meta) if meta else None}

    steps = {
        step: AsyncResult(f"{external_job_id}:{step}", app=celery_app).state
        for step in STEP_NAMES
    }
    # a requested callback is marked WAITING at submit time, so PENDING here means none was asked for
    callback_result = AsyncResult(f"{external_job_id}:callback-final", app=celery_app)
    callback_state = callback_result.state
    callback_requested = callback_state != "PENDING"
    callback_info = callback_result.info
    if isinstance(callback_info, CallbackError):
        # FAILURE / RETRY: the portal's HTTP status (None if it was never reached)
        message, http_status = callback_info.args
        callback_info = {"msg": message, "http_status": http_status}
    elif not isinstance(callback_info, dict):
        callback_info = None

    # Celery-side tasks (not the PBS job); PENDING also means not started / not requested / expired
    return job_response(
        task_id=external_job_id,
        status=worker_result.state,
        result={
            **meta,
            "steps": steps,
            "callback": {
                "requested": callback_requested,
                "status": callback_state if callback_requested else "NOT_REQUESTED",
                "info": callback_info,
            },
        },
        job_type="transform",
        time=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/log/{external_job_id}")
async def get_log(external_job_id: str, issuer: str = Query(None)) -> Dict[str, Any]:
    """
    Retrieve the processing log for a transformation job.

    Checks if the JSONL log file exists for the given external_job_id. Returns three possible
    responses:
    1. Log exists: returns jsonl content as a list of JSON objects
    2. Log exists but parsed failed: returns error indicating invalid JSON
    3. No log found: returns no record found

    Parameters
    ----------
    external_job_id : str
        The unique request identifier (UUID) to retrieve logs for.
    issuer : str, optional
        Email or identifier of the person who initiated the request.

    Returns
    -------
    dict
        Standardized JSON response with request ID and parsed log entries and status message.

    Examples
    --------
    >>> curl -X GET https://api_url/compose/log/550e8400-e29b-41d4-a716-446655440000
    >>> curl -X GET "https://api_url/compose/log/550e8400-e29b-41d4-a716-446655440000?issuer=user@ucar.edu"
    """
    log_path = Path(WORKDIR) / f"{external_job_id}.gdexws.jsonl"

    # Case 1: Log file exists
    if log_path.exists():
        try:
            # Parse JSONL file (each line is a separate JSON object)
            log_entries = []
            with open(log_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            log_entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            log_entries.append({"error": "Invalid JSON", "raw": line})

            response = {
                "external_job_id": external_job_id,
                "log_entries": log_entries,
                "status_message": "Log retrieved successfully"
            }
            if issuer:
                response["issuer"] = issuer
            return response
        except Exception as e:
            return {
                "external_job_id": external_job_id,
                "status_message": f"Error reading log: {str(e)}"
            }

    # Case 2: Log not found
    return {
        "external_job_id": external_job_id,
        "status_message": "Request log not available"
    }



@router.post("/transform")
def post_transform(
    request: TransformRequest,
    issuer: str = Query(None),
    specialist: str = Query("chiaweih")
) -> Dict[str, Any]:
    """
    Submit transformation job for dscheck processing.

    Validates the request and enqueues it on the Celery worker; the Boreas
    uploads and dscheck inserts happen off the request thread.

    Parameters
    ----------
    request : TransformRequest
        Request object containing files and transformation commands.

        Attributes:
            files : List[str]
                List of relative file paths to process.
            commands : List[Command]
                List of transformation commands. Each command must have:
                - command: str - the operation type
                - Additional key-value pairs for command-specific parameters
            callback_url : str, optional
                ("CallbackUrl" in JSON) URL to POST the final result to
                when the PBS job finishes.
    issuer : str, optional
        Email or identifier of the person who initiated the request.
    specialist : str, optional
        Specialist assigned to process the job. Default: "chiaweih"

    Returns
    -------
    dict => this returns the external job ID to the web portal for data table lookup on AsyncTask
        - external_job_id: Request identifier (UUID) 
        - issuer: Issuer identifier (if provided)
        - status_message: Human-readable status message
        - status_url: GET endpoint to poll for job state
        - log_url: GET endpoint for the full job log

    Examples
    --------
    >>> curl -X POST https://gdex-services.k8s.ucar.edu/compose/transform \\
           -H "Content-Type: application/json" \\
           -d '{
             "Files": ["Web-services/test.nc"],
             "Commands": [
               {
                 "command": "add_global_meta",
                 "global-attr-name": "gdex_dsid",
                 "global-attr-value": "d99ext9",
                 "debug": true
               }
             ],
             "CallbackUrl": ""
           }'

    JSON payload example for the request:
    ```json
    {
        "Files": ["Web-services/test.nc"],
        "Commands": [
            {
                "command": "add_global_meta",
                "global-attr-name": "gdex_dsid",
                "global-attr-value": "d99ext9",
                "debug": true
            }
        ],
        "CallbackUrl": "https://gdex.ucar.edu/api/gdex-services-event/"
    }
    ```
    
    """
    # Generate external job ID for tracking the transform request
    external_job_id = str(uuid.uuid4())

    # refuse access if callback URL is provided but not in the listed allowed URL
    #  avoid using callback URL to unauthorized endpoints
    if request.callback_url == "":
        request.callback_url = None

    if request.callback_url is not None and request.callback_url not in ALLOWED_CALLBACK_URLS:
        return JSONResponse(
            status_code=403,
            content={"detail": "Callback URL not allowed"}
        )

    # fail fast: refuse any file path that escapes the exchange directory
    #  before anything is queued or any callback is sent
    path_check = relpath_validate(request.files)
    if path_check is not True:
        return JSONResponse(status_code=403, content=path_check)

    try:
        # Queue the transform job with Celery
        # maintain the PascalCase format on key name to match the expected JSON structure that pass to the celery worker
        celery_transform(request.model_dump(by_alias=True), external_job_id, specialist)
        # Only place external_job_id is tied to who submitted it (not in the access log)
        logger.info(
            f"Transform queued external_job_id={external_job_id} issuer={issuer} specialist={specialist} "
            f"files={len(request.files)} callback={'yes' if request.callback_url else 'no'}"
        )
    except Exception as e:
        # ex: Broker (Redis) unreachable — nothing was enqueued
        logger.exception(f"Failed to queue transform external_job_id={external_job_id} with exception: {e}")
        raise HTTPException(status_code=503, detail="Failed to queue transform job")

    return {
        "external_job_id": external_job_id,
        "issuer": issuer,
        "status_message": "Transform job accepted and queued",
        "status_url": f"/compose/status/{external_job_id}",
        "log_url": f"/compose/log/{external_job_id}",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """Simple health check for the test API."""
    return {
        "status": "healthy",
        "service": "gdex-dscheck-test-api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rda_available": RDA_AVAILABLE
    }
