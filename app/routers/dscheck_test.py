"""
Simple test API for retrieving dscheck information.

GET-only endpoint with hardcoded query:
- Table: dscheck
- Condition: specialist = 'chiaweih'
- Fields: * (all)

SECURITY: Test endpoint only. Restrict access in production.
"""
from fastapi import APIRouter
from typing import Dict, Any
from datetime import datetime


# Import RDA/GDEX libraries
try:
    from rda_python_common.pg_dbi import PgDBI
    RDA_AVAILABLE = True
except ImportError as e:
    raise RuntimeError(
        f"Failed to import rda_python_common: {e}\n"
        "Install with: pip install rda-python-common"
    ) from e


router = APIRouter(prefix="/dscheck", tags=["dscheck_testing"])

@router.get("/getinfo1")
async def get_dscheck_info() -> Dict[str, Any]:
    """
    Retrieve dscheck information for specialist 'chiaweih'.

    **SECURITY**: This is a test endpoint only. Hardcoded values, no parameters.

    Query:
        PgDBI.pgget("dscheck", "*", "specialist = 'chiaweih'", logact|PgLOG.EXITLG)

    Returns:
        Single dscheck record with all fields or error message

    Example:
        GET /dscheck/getinfo1 (full path after router prefix)
    """
    try:
        # Hardcoded query as specified
        specialist = "chiaweih"
        condition = f"specialist = '{specialist}'"

        # Create PgDBI instance and query single record using pgget
        db = PgDBI()
        record = db.pgget("dscheck", "*", condition, db.PGLOG['LOGMASK'])

        if not record:
            return {
                "success": False,
                "message": f"No dscheck record found for specialist '{specialist}'",
                "timestamp": datetime.now().isoformat(),
                "specialist": specialist,
                "record": None
            }

        # Extract and format first key-value pair
        first_key, first_value = next(iter(record.items()))

        if hasattr(first_value, 'isoformat'):
            first_value = first_value.isoformat()

        response_record = {first_key: first_value}

        return {
            "success": True,
            "message": f"Successfully retrieved dscheck record for specialist '{specialist}'",
            "timestamp": datetime.now().isoformat(),
            "specialist": specialist,
            "record": response_record
        }

    except Exception as e:
        error_msg = str(e)
        return {
            "success": False,
            "message": "Failed to retrieve dscheck information",
            "timestamp": datetime.now().isoformat(),
            "specialist": "chiaweih",
            "record": None,
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
