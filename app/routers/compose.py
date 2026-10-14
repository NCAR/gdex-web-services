"""
The router module for compose endpoints in the gdex-web-services FastAPI application.

This module defines endpoints for compose action (i.e. transform ... etc), and including status retrieval.

"""
from typing import Dict, Any
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from celery.result import AsyncResult
import uuid
import json
import logging

from app.schemas.models import TransformRequest
from app.celery_app import celery_app
from app.tasks.transform_tasks import celery_transform
from app.utils import get_dscheck_json

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
# GET /status/{request_id} below instead of failing int validation here.
@router.get("/status/{cindex:int}")
async def get_status(cindex: int) -> Dict[str, Any]:
    """
    Retrieve dscheck status and latest processing output for a specific cindex.

    Queue-level debugging tool. Clients should poll
    GET /compose/status/{request_id} instead.

    Queries the dscheck database record for the given cindex. Returns
    standardized JSON with the record's current status. For the job's own
    processing log, see GET /compose/log/{request_id}.

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


@router.get("/status/{request_id}")
def get_celery_status(request_id: str) -> Dict[str, Any]:
    """
    Retrieve Redis-backed job status for a transformation job.

    Cheap enough to poll every few seconds. For the full step-by-step history,
    use GET /compose/log/{request_id}; for raw dscheck-row inspection, use
    GET /compose/status/{cindex}.

    Parameters
    ----------
    request_id : str
        The unique request identifier (UUID) returned by POST /compose/transform.

    Returns
    -------
    dict
        - request_id: Request identifier
        - state: PENDING (log not written yet, or unknown request_id),
          PBS_PROGRESS, PBS_FAILURE_REPORTED (the job logged an ERROR line),
          SUCCESS (job finished; see info.job_status = completed|failed),
          or FAILURE (the status watcher itself broke)
        - info: The latest JSONL log entry (gdexws log_format dict), the
          final result on SUCCESS, or an error string on FAILURE

    Examples
    --------
    >>> curl -X GET https://api_url/compose/status/550e8400-e29b-41d4-a716-446655440000
    """
    result = AsyncResult(request_id, app=celery_app)
    info = result.info
    return {
        "request_id": request_id,
        "state": result.state,
        "info": info if isinstance(info, dict) or info is None else str(info),
    }


@router.get("/log/{request_id}")
async def get_log(request_id: str, issuer: str = Query(None)) -> Dict[str, Any]:
    """
    Retrieve the processing log for a transformation job.

    Checks if the JSONL log file exists for the given request_id. Returns three possible
    responses:
    1. Log exists: returns jsonl content as a list of JSON objects
    2. Log exists but parsed failed: returns error indicating invalid JSON
    3. No log found: returns no record found

    Parameters
    ----------
    request_id : str
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
    log_path = Path(WORKDIR) / f"{request_id}.gdexws.jsonl"

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
                "request_id": request_id,
                "log_entries": log_entries,
                "status_message": "Log retrieved successfully"
            }
            if issuer:
                response["issuer"] = issuer
            return response
        except Exception as e:
            return {
                "request_id": request_id,
                "status_message": f"Error reading log: {str(e)}"
            }

    # Case 2: Log not found
    return {
        "request_id": request_id,
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
    dict
        - request_id: Request identifier (UUID)
        - issuer: Issuer identifier (if provided)
        - status_message: Human-readable status message
        - status_url: GET endpoint to poll for job state
        - log_url: GET endpoint for the full job log

    Examples
    --------
    >>> curl -X POST https://0.0.0.0:8080/compose/transform \\
    ...   -H "Content-Type: application/json" \\
    ...   -d '{
    ...     "Files": ["Web-services/test.nc"],
    ...     "Commands": [
    ...       {
    ...         "command": "add_global_meta",
    ...         "global-attr-name": "gdex_dsid",
    ...         "global-attr-value": "d99ext9",
    ...         "debug": true
    ...       },
    ...     ],
    ...     "CallbackUrl": "https://example.com/callback"
    ...   }'
    
    """
    request_id = str(uuid.uuid4())
    try:
        # Queue the transform job with Celery
        # maintain the PascalCase format on key name to match the expected JSON structure that pass to the celery worker
        celery_transform(request.model_dump(by_alias=True), request_id, specialist)
        # Only place request_id is tied to who submitted it (not in the access log)
        logger.info(
            f"Transform queued request_id={request_id} issuer={issuer} specialist={specialist} "
            f"files={len(request.files)} callback={'yes' if request.callback_url else 'no'}"
        )
    except Exception as e:
        # Broker (Redis) unreachable — nothing was enqueued
        logger.exception(f"Failed to queue transform request_id={request_id}")
        raise HTTPException(status_code=503, detail=f"Failed to queue transform job: {e}") from e

    return {
        "request_id": request_id,
        "issuer": issuer,
        "status_message": "Transform job accepted and queued",
        "status_url": f"/compose/status/{request_id}",
        "log_url": f"/compose/log/{request_id}",
    }


@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """Simple health check for the test API."""
    return {
        "status": "healthy",
        "service": "gdex-dscheck-test-api",
        "timestamp": datetime.now().isoformat(),
        "rda_available": RDA_AVAILABLE
    }
