"""
Simple test API for retrieving dscheck information.

GET-only endpoint with hardcoded query:
- Table: dscheck
- Condition: specialist = 'chiaweih'
- Fields: * (all)

SECURITY: Test endpoint only. Restrict access in production.
"""

import subprocess
from typing import Dict, Any
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter
from pydantic import BaseModel, field_validator



# Import RDA/GDEX libraries
try:
    from rda_python_common.pg_dbi import PgDBI
    from rda_python_common.pg_log import PgLOG
    RDA_AVAILABLE = True
except ImportError as e:
    raise RuntimeError(
        f"Failed to import rda_python_common: {e}\n"
        "Install with: pip install rda-python-common"
    ) from e


router = APIRouter(prefix="/dscheck", tags=["dscheck_testing"])


class MetadataRequest(BaseModel):
    """Request model for metadata modification."""
    data_file_relpath: Path = Path('Web-services/test.nc')
    attr_name: str
    attr_value: str

    @field_validator('data_file_relpath')
    @classmethod
    def validate_relative_path(cls, v: Path) -> Path:
        """Ensure path is relative and doesn't escape base directory."""
        if v.is_absolute():
            raise ValueError(f"Path must be relative, got absolute path: {v}")

        # Prevent path traversal attacks (../)
        if ".." in str(v):
            raise ValueError(f"Path traversal not allowed: {v}")

        return v


@router.get("/status/{cindex}")
async def get_status_jsonl(cindex: int) -> Dict[str, Any]:
    """
    Retrieve dscheck status and latest output log located in 
    /glade/campaign/collections/gdex/decsdata/gdex-web-services-log
    for specific cindex.

    Returns
    -------
    dict
        Response dictionary containing:

        - success (bool): Whether the query succeeded
        - message (str): Human-readable status message
        - timestamp (str): ISO format timestamp
        - specialist (str): The specialist name queried
        - record (dict): The first dscheck record matching the query, or None
        - error (str, optional): Error message if query failed


    Examples
    --------
    >>> curl -X GET https://api_url/dscheck/status/4071816
    {"success": true, "message": "dscheck record found...", ...}
    """
    try:
        # Hardcoded query as specified
        condition = f"cindex = {cindex}"

        # Create PgDBI instance and query single record using pgget
        db = PgDBI()
        record = db.pgget("dscheck", "*", condition, db.PGLOG['LOGMASK'])

        if not record:
            return {
                "success": False,
                "message": f"No dscheck record found for cindex '{cindex}'",
                "timestamp": datetime.now().isoformat(),
                "cindex": cindex,
                "record": None
            }

        # get status from the dscheck table
        record_status = record.get('STATUS', 'Unknown')
        if record_status == 'C':
            message = f"dscheck record for cindex '{cindex}' is queued for execution."
            return {
                "success": False,
                "message": message,
                "timestamp": datetime.now().isoformat(),
                "cindex": cindex,
                "record": record
            }
        elif record_status == 'R':
            message = f"dscheck record for cindex '{cindex}' is currently running."
            # read /glade/campaign/collections/gdex/decsdata/gdex-web-services-log
            latest_log_path = f"/glade/campaign/collections/gdex/data/exchange/Web-services/{cindex}.jsonl"
            try:
                result = subprocess.run(
                    ["tail", "-1", latest_log_path],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True
                )
                latest_log = result.stdout.strip() if result.returncode == 0 else "No log data found."
            except FileNotFoundError:
                latest_log = "Log file not found."
            except subprocess.TimeoutExpired:
                latest_log = "Log read timeout."
            except Exception as e:
                latest_log = f"Error reading log: {str(e)}"

            return {
                "success": False,
                "message": message,
                "timestamp": datetime.now().isoformat(),
                "cindex": cindex,
                "record": record,
                "latest_log": latest_log
            }
        else:
            message = f"dscheck record for cindex '{cindex}' has status: {record_status}"
            return {
                "success": True,
                "message": message,
                "timestamp": datetime.now().isoformat(),
                "cindex": cindex,
                "record": record
            }

    except Exception as e:
        error_msg = str(e)
        return {
            "success": False,
            "message": "Failed to retrieve status",
            "timestamp": datetime.now().isoformat(),
            "specialist": "chiaweih",
            "record": None,
            "error": error_msg
        }



@router.post("/metadata")
async def submit_metadata_modify(
    request: MetadataRequest
) -> Dict[str, Any]:
    """
    Submit metadata modification for dscheck records.

    Parameters
    ----------
    request : MetadataRequest
        Request object containing metadata modification parameters.

        Attributes:
            data_file_relpath : Path
                Relative path to the data file. Must be relative, not absolute.
                Default: 'Web-services/test.nc'
            attr_name : str
                Name of the attribute to modify.
                Default: 'gdex_dsid'
            attr_value : str
                Value of the attribute to set.
                Default: 'd99ext9'

    Returns
    -------
    dict
        Response dictionary containing:

        - success (bool): Whether the operation succeeded
        - message (str): Human-readable status message
        - timestamp (str): ISO format timestamp
        - record (dict): The modified dscheck record with cindex
        - error (str, optional): Error message if operation failed

    Raises
    ------
    ValueError
        If data_file_relpath is an absolute path instead of relative.
    Exception
        Database operation failures are caught and returned in response.

    Examples
    --------
    >>> curl -X POST https://api_url/dscheck/metadata \\
    ...   -H "Content-Type: application/json" \\
    ...   -d '{
    ...     "data_file_relpath": "exchange_subfolder/test.nc",
    ...     "attr_name": "gdex_dsid",
    ...     "attr_value": "d99ext9"
    ...   }'
    {"success": true, "message": "Successfully added dscheck record...", ...}
    """
    # Initialize dictionary with None values for all expected fields (can extend as needed)
    dict_dscheck_post = {
        'command': None,
        'specialist': None,
        'argv': None,
        'workdir': None,
    }

    # SECURITY: Define data file path with validation
    base_dir = Path('/gdex/data/exchange/').resolve()
    data_file_path = (base_dir / request.data_file_relpath).resolve()

    # Verify final resolved path is within base directory (prevent path traversal)
    try:
        data_file_path.relative_to(base_dir)
    except ValueError:
        return {
            "success": False,
            "message": "Access denied: Path escapes base directory",
            "timestamp": datetime.now().isoformat(),
            "record": dict_dscheck_post,
            "error": "Path traversal attempt blocked"
        }

    try:
        # Hardcoded shell script for now as specified
        command = 'add_global_attr.pbs'
        specialist = "chiaweih"

        # Use relative paths with pathlib.Path
        workdir_path = Path('/glade/u/home/chiaweih/data_curation_script/')
        argv = f"{data_file_path} {request.attr_name} {request.attr_value}"
        workdir = str(workdir_path)

        # Update dictionary with hardcoded values
        dict_dscheck_post['command'] = command
        dict_dscheck_post['specialist'] = specialist
        dict_dscheck_post['argv'] = argv
        dict_dscheck_post['workdir'] = workdir
        dict_dscheck_post['status'] = "R" # force no run status to get cindex first

        # Create PgDBI instance and query single record using pgget
        db = PgDBI()
        cindex = db.pgadd("dscheck", dict_dscheck_post, PgLOG.EXITLG|PgLOG.AUTOID|PgLOG.DODFLT)

        # check output status and return appropriate response
        if cindex > 0:
            # Update record with environment variables so when the script runs, it can access the cindex value
            env_vars = f"CINDEX={cindex}"
            record = {"environments": env_vars, 'status': "C"}
            dict_dscheck_post['status'] = "C" # force queue status after env variable CINDEX is set
            db.pgupdt("dscheck", record, f"cindex = {cindex}", db.PGLOG['LOGMASK'])
            # update record
            dict_dscheck_post['cindex'] = cindex
            dict_dscheck_post['environments'] = env_vars

            return {
                "success": True,
                "message": f"Successfully added dscheck record with cindex '{cindex}'",
                "timestamp": datetime.now().isoformat(),
                "record": dict_dscheck_post
            }
        else:
            log = PgLOG()
            log.pglog("Fail to add dscheck record for '{}'".format(dict_dscheck_post['command']), logact=PgLOG.RETMSG)
            return {
                "success": False,
                "message": "Failed to add dscheck information, no cindex returned",
                "timestamp": datetime.now().isoformat(),
                "record": dict_dscheck_post,
                "error": "Failed to add dscheck record"
            }

    except Exception as e:
        error_msg = str(e)
        return {
            "success": False,
            "message": "Failed to add dscheck information, during exception",
            "timestamp": datetime.now().isoformat(),
            "record": dict_dscheck_post,
            "error": error_msg
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
