"""
The router module for compose endpoints in the gdex-web-services FastAPI application.

This module defines endpoints for compose action (i.e. transform ... etc), and including status retrieval.

"""
from typing import Dict, Any
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Query, BackgroundTasks
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


@router.get("/log/{cindex}")
async def get_log(cindex: int, issuer: str = Query(None)) -> Dict[str, Any]:
    """
    Retrieve the processing log for a transformation job.

    Checks if the JSONL log file exists for the given cindex. Returns three possible
    responses:
    1. Log exists: returns dscheck record with parsed log entries
    2. cindex record exists but log not ready: returns dscheck record with status message
    3. No cindex record and no log: returns error indicating no record found

    Parameters
    ----------
    cindex : int
        The dscheck record index to retrieve logs for.
    issuer : str, optional
        Email or identifier of the person who initiated the request.

    Returns
    -------
    dict
        Standardized dscheck JSON response with status and parsed log entries.

    Examples
    --------
    >>> curl -X GET https://api_url/compose/log/4071816
    >>> curl -X GET "https://api_url/compose/log/4071816?issuer=user@ucar.edu"
    """
    log_path = Path(WORKDIR) / f"{cindex}.gdexws.jsonl"

    # Case 1: Log file exists
    if log_path.exists():
        try:
            response = get_dscheck_json(cindex, issuer=issuer)

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

            response["log_entries"] = log_entries
            return response
        except Exception as e:
            return get_dscheck_json(cindex=cindex, issuer=issuer, status_message=f"Error reading log: {str(e)}")

    # Case 2 & 3: Check if cindex record exists in database
    response = get_dscheck_json(cindex, issuer=issuer)

    # If we got a valid dscheck record, log is just not ready yet
    if response.get("cindex") and response["cindex"] > 0:
        response["status_message"] = "Processing in progress, log not yet available"
        return response

    # Case 3: No cindex record and no log
    return get_dscheck_json(cindex=0, issuer=issuer, status_message=f"No record found for cindex {cindex}")



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
    # # Validate file paths against base directory
    # validation_result = relpath_validate(request.files)
    # if validation_result is not True:
    #     return validation_result

    # Create and upload payload to Boreas
    payload_url = create_transform_payload(request)

    # Create and upload PBS script to Boreas
    pbs_script = create_pbs_script(payload_url)

    # Prepare the dscheck record for submission for pbs script download
    dict_dscheck_post = {
        'command': 'curl',
        'specialist': specialist,
        'argv': f'-o transform.pbs "{pbs_script}"',
        'workdir': WORKDIR,
        'status': 'R',
    }
    
    try:
        # Create PgDBI instance and add record
        db = PgDBI()
        cindex_pbs = db.pgadd("dscheck", dict_dscheck_post, PgLOG.EXITLG|PgLOG.AUTOID|PgLOG.DODFLT)

        if cindex_pbs > 0:
            # Update record with environment variables so when the script runs, it can access the cindex value
            env_vars = f"CINDEX={cindex_pbs}"
            record = {
                "environments": env_vars, 
                'argv': f'-o {cindex_pbs}.pbs "{pbs_script}"',
                'status': "C"
            }
            db.pgupdt("dscheck", record, f"cindex = {cindex_pbs}", db.PGLOG['LOGMASK'])
            # return get_dscheck_json(cindex=cindex_pbs, status_message="PBS downloading")
        else:
            log = PgLOG()
            log.pglog("Fail to add dscheck record for '{}'".format(dict_dscheck_post['command']), logact=PgLOG.RETMSG)
            return get_dscheck_json(cindex=0, status_message="No cindex returned for download PBS script")

    except Exception as e:
        error_msg = str(e)
        return get_dscheck_json(cindex=0, status_message="Failed on dscheck update info") | {"error": error_msg}

    # Submit the PBS script for execution in the background after it is downloaded, to avoid race condition
    background_tasks.add_task(
        pbs_submit,
        cindex_pbs,
        specialist
    )

    # do not wait for the pbs_submit to finish, return the cindex_pbs for the user to check the status
    return get_dscheck_json(cindex=cindex_pbs, status_message=f"PBS script downloading + queued for execution")

async def pbs_submit(cindex_pbs: int, specialist: str) -> Dict[str, Any]:
    """
    The async function to submit the PBS script for execution after it is downloaded.

    Parameters
    ----------
    cindex_pbs : int
        The cindex of the dscheck record for the PBS script download.
    specialist : str
        The specialist assigned to process the job.

    """
    # check if the pbs script is downloaded successfully
    # pbs_script_path = Path(WORKDIR) / f"{cindex_pbs}.pbs"
    # retry till it becomes available, or timeout after 3 mins

    # # local test
    # print('Check for 3 mins, waiting for the pbs script to be downloaded to avoid race condition',flush=True)
    # timeout = 60*1
    # await asyncio.sleep(timeout)

    # k8s deployment
    timeout = 60*3
    start_time = time.time()
    pbs_script_path = Path(WORKDIR) / f"{cindex_pbs}.pbs"
    while not pbs_script_path.exists():
        if time.time() - start_time > timeout:
            return get_dscheck_json(cindex=0, status_message=f"Failed on downloading PBS script at {pbs_script_path}")
        await asyncio.sleep(10)
    # return get_dscheck_json(cindex=0, status_message=f"PBS script downloaded successfully at {pbs_script_path}")


    # Prepare the dscheck record for submission for pbs script download
    # CINDEX is pass as env var directly pass through the pbs with
    # single dscheck record with no update due to using the previous cindex_pbs
    # this make it easier to track the status of the transform job with a single cindex
    dict_dscheck_post = {
        'command': 'qsub',
        'specialist': specialist,
        'argv': f'-v CINDEX={cindex_pbs} {cindex_pbs}.pbs',
        'workdir': WORKDIR
    }
    
    try:
        # Create PgDBI instance and add record
        db = PgDBI()
        cindex_transform = db.pgadd("dscheck", dict_dscheck_post, PgLOG.EXITLG|PgLOG.AUTOID|PgLOG.DODFLT)

        # if cindex_transform > 0:
        #     # Update record with environment variables so when the script runs, it can access the cindex value
        #     env_vars = f"BACKGROUND_CINDEX={cindex_transform}"
        #     record = {
        #         'environments': env_vars,
        #         'argv': f'-v CINDEX={cindex_pbs},BACKGROUND_CINDEX={cindex_transform} {cindex_pbs}.pbs'
        #     }
        #     # add the background cindex to the argv so that the pbs script can access it and update the status of the transform job
        #     db.pgupdt("dscheck", record, f"cindex = {cindex_pbs}", db.PGLOG['LOGMASK'])

        # else:
        #     log = PgLOG()
        #     log.pglog("Fail to add dscheck record for '{}'".format(dict_dscheck_post['command']), logact=PgLOG.RETMSG)
        #     return get_dscheck_json(cindex=0, status_message="No cindex returned for download PBS script")

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
