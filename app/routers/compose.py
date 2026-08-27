"""
The router module for compose endpoints in the gdex-web-services FastAPI application.

This module defines endpoints for compose action (i.e. transform ... etc), and including status retrieval.

"""
from typing import Dict, Any
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Query
import json

from app.schemas import TransformRequest
from app.utils import get_dscheck_json, relpath_validate

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



@router.post("/transform")
async def post_transform(
    # request: TransformRequest,
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

    # setup where the pbs script is temperarily located on HPC
    workdir = str(Path('/glade/campaign/collections/gdex/data/exchange/Web-services/'))

    # for testing use the payload already on Boreas
    # TODO: need to get the request and save payload to the Boreas server 
    payload_json = "https://boreas.hpc.ucar.edu:6443/gdex-data/services_tmp/payloads/transform.payload.json"

    # for testing use the pbs script already on Boreas
    # TODO: need to get the request and generate and save pbs script to the Boreas server
    pbs_script = "https://boreas.hpc.ucar.edu:6443/gdex-data/services_tmp/pbs/transform.pbs"

    # Prepare the dscheck record for submission for pbs script download
    dict_dscheck_post = {
        'command': 'curl',
        'specialist': specialist,
        'argv': f'-o transform.pbs "{pbs_script}"',
        'workdir': workdir,
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
            return get_dscheck_json(cindex=cindex_pbs, status_message="No cindex returned for download PBS script")

    except Exception as e:
        error_msg = str(e)
        return get_dscheck_json(cindex=cindex_pbs, status_message="Failed on dscheck update info") | {"error": error_msg}

    # check if the pbs script is downloaded successfully
    pbs_script_path = Path(workdir) / f"{cindex_pbs}.pbs"
    # retry till it becomes available, or timeout after 3 mins
    import time
    timeout = 60*3
    start_time = time.time()
    while not pbs_script_path.exists():
        if time.time() - start_time > timeout:
            return get_dscheck_json(cindex=cindex_pbs, status_message="Failed on downloading PBS script")
        time.sleep(10)


    # Prepare the dscheck record for submission for pbs script download
    dict_dscheck_post = {
        'command': f'{cindex_pbs}.pbs',
        'specialist': specialist,
        'argv': payload_json,
        'workdir': workdir,
        'status': 'R',
    }
    
    try:
        # Create PgDBI instance and add record
        db = PgDBI()
        cindex_transform = db.pgadd("dscheck", dict_dscheck_post, PgLOG.EXITLG|PgLOG.AUTOID|PgLOG.DODFLT)

        if cindex_transform > 0:
            # Update record with environment variables so when the script runs, it can access the cindex value
            env_vars = f"CINDEX={cindex_transform}"
            record = {
                "environments": env_vars, 
                'status': "C"
            }
            db.pgupdt("dscheck", record, f"cindex = {cindex_transform}", db.PGLOG['LOGMASK'])
            return get_dscheck_json(cindex=cindex_transform, status_message="PBS running")
        else:
            log = PgLOG()
            log.pglog("Fail to add dscheck record for '{}'".format(dict_dscheck_post['command']), logact=PgLOG.RETMSG)
            return get_dscheck_json(cindex=cindex_transform, status_message="No cindex returned for download PBS script")

    except Exception as e:
        error_msg = str(e)
        return get_dscheck_json(cindex=cindex_pbs, status_message="Failed to download PBS script") | {"error": error_msg}

@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """Simple health check for the test API."""
    return {
        "status": "healthy",
        "service": "gdex-dscheck-test-api",
        "timestamp": datetime.now().isoformat(),
        "rda_available": RDA_AVAILABLE
    }
