"""
The router module for compose endpoints in the gdex-web-services FastAPI application.

This module defines endpoints for compose action (i.e. transform ... etc), and including status retrieval.

"""
from typing import Dict, Any
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Query, BackgroundTasks
import uuid
import time
import asyncio
import json

from app.schemas.models import TransformRequest
from app.utils import get_dscheck_json, create_transform_payload, create_pbs_script

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

WORKDIR = str(Path('/glade/campaign/collections/gdex/data/exchange/Web-services/'))


@router.get("/status/{cindex}")
async def get_status(cindex: int) -> Dict[str, Any]:
    """
    Retrieve dscheck status and latest processing output for a specific cindex.

    Queries the dscheck database record and retrieves the latest log entry
    from the corresponding JSONL file. Returns standardized JSON with current
    status and processing information.

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
        - processing_status: Latest log entry from JSONL
        - error: Error message (if applicable)

    Examples
    --------
    >>> curl -X GET https://api_url/compose/status/4071816
    >>> curl -X GET "https://api_url/compose/status/4071816?issuer=user@ucar.edu"
    """
    return get_dscheck_json(cindex)


@router.get("/log/{request_id}")
async def get_log(request_id: str, issuer: str = Query(None)) -> Dict[str, Any]:
    """
    Retrieve the processing log for a transformation job.

    Checks if the JSONL log file exists for the given request_id. Returns three possible
    responses:
    1. Log exists: returns dscheck record with parsed log entries
    2. No log file yet: returns status message
    3. No log found: returns error indicating no record found

    Parameters
    ----------
    request_id : str
        The unique request identifier (UUID) to retrieve logs for.
    issuer : str, optional
        Email or identifier of the person who initiated the request.

    Returns
    -------
    dict
        Standardized dscheck JSON response with status and parsed log entries.

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

    # Case 2: Log not ready yet
    return {
        "request_id": request_id,
        "status_message": "Processing in progress, log not yet available"
    }



@router.post("/transform")
async def post_transform(
    request: TransformRequest,
    background_tasks: BackgroundTasks,
    issuer: str = Query(None),
    specialist: str = Query("chiaweih")
) -> Dict[str, Any]:
    """
    Submit transformation job for dscheck processing.

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
    issuer : str, optional
        Email or identifier of the person who initiated the request.
    specialist : str, optional
        Specialist assigned to process the job. Default: "chiaweih"

    Returns
    -------
    dict
        Standardized dscheck JSON response.

    Examples
    --------
    >>> curl -X POST https://api_url/compose/transform \\
    ...   -H "Content-Type: application/json" \\
    ...   -d '{
    ...     "Files": ["Web-services/test.nc"],
    ...     "Commands": [
    ...       {
    ...         "command": "add_global_meta",
    ...         "global-attr-name": "gdex_dsid",
    ...         "global-attr-value": "d99ext9",
    ...         "debug": true
    ...       }
    ...     ]
    ...   }'
    """
    # Generate unique request ID
    request_id = str(uuid.uuid4())

    # Create and upload payload to Boreas with request_id in filename
    # Format: services_tmp/payloads/transform.payload.{request_id}.json
    payload_url = create_transform_payload(request, request_id=request_id)

    # Create and upload PBS script to Boreas with request_id in filename
    # Format: services_tmp/pbs/transform.{request_id}.pbs
    pbs_script = create_pbs_script(payload_url, request_id=request_id)

    # Prepare the dscheck record for submission for pbs script download
    dict_dscheck_post = {
        'command': 'curl',
        'specialist': specialist,
        # Download PBS script and save locally as: transform.{request_id}.pbs
        'argv': f'-o transform.{request_id}.pbs "{pbs_script}"',
        'workdir': WORKDIR
    }
    
    try:
        # Create PgDBI instance and add record
        db = PgDBI()
        cindex_download = db.pgadd("dscheck", dict_dscheck_post, PgLOG.EXITLG|PgLOG.AUTOID|PgLOG.DODFLT)

        if cindex_download <= 0:
            log = PgLOG()
            log.pglog("Fail to add dscheck record for '{}'".format(dict_dscheck_post['command']), logact=PgLOG.RETMSG)
            return get_dscheck_json(cindex=0, status_message="No cindex returned for download PBS script")

    except Exception as e:
        error_msg = str(e)
        return get_dscheck_json(cindex=0, status_message="Failed on dscheck update info") | {"error": error_msg}

    # Submit the PBS script for execution in the background after it is downloaded, to avoid race condition
    background_tasks.add_task(
        pbs_submit,
        specialist,
        request_id
    )

    # do not wait for the pbs_submit to finish, return the cindex_pbs for the user to check the status
    return get_dscheck_json(cindex=cindex_download, request_id=request_id, status_message=f"PBS script downloading + queued for execution")

async def pbs_submit(specialist: str, request_id: str, workdir: str = WORKDIR) -> Dict[str, Any]:
    """
    The async function to submit the PBS script for execution after it is downloaded.

    Parameters
    ----------
    request_id : str
        The unique request identifier used in filename.
    specialist : str
        The specialist assigned to process the job.
    workdir : str
        The working directory where the PBS script is located.

    """
    # check if the pbs script is downloaded successfully
    # retry till it becomes available, or timeout after 3 mins

    # # local test
    # timeout = 60*1
    # await asyncio.sleep(timeout)

    # k8s deployment
    timeout = 60*3
    start_time = time.time()
    pbs_script_path = Path(workdir) / f"transform.{request_id}.pbs"
    while not pbs_script_path.exists():
        if time.time() - start_time > timeout:
            return get_dscheck_json(cindex=0, status_message=f"Failed on downloading PBS script at {pbs_script_path}")
        await asyncio.sleep(10)
    # return get_dscheck_json(cindex=0, status_message=f"PBS script downloaded successfully at {pbs_script_path}")


    # Prepare the dscheck record for submission for pbs script download
    # REQUEST_ID is passed as env var to PBS script for JSONL filename
    dict_dscheck_post = {
        'command': 'qsub',
        'specialist': specialist,
        'argv': f'-v REQUEST_ID={request_id} transform.{request_id}.pbs',
        'workdir': workdir
    }
    
    try:
        # Create PgDBI instance and add record
        db = PgDBI()
        cindex_submit = db.pgadd("dscheck", dict_dscheck_post, PgLOG.EXITLG|PgLOG.AUTOID|PgLOG.DODFLT)

    except Exception as e:
        error_msg = str(e)
        raise RuntimeError(f"Failed to submit PBS script: {error_msg}") from e



@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """Simple health check for the test API."""
    return {
        "status": "healthy",
        "service": "gdex-dscheck-test-api",
        "timestamp": datetime.now().isoformat(),
        "rda_available": RDA_AVAILABLE
    }
