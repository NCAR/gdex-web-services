"""Utility functions for dscheck record formatting and retrieval."""

from datetime import datetime
from typing import Dict, Any, Optional

try:
    from rda_python_common.pg_dbi import PgDBI
except ImportError as e:
    raise RuntimeError(
        f"Failed to import rda_python_common: {e}\n"
        "Install with: pip install rda-python-common"
    ) from e


def get_dscheck_json(cindex: int = 0, issuer: Optional[str] = None, status_message: Optional[str] = None, request_id: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve and format a dscheck record.

    Queries the dscheck database table for the given cindex and returns a
    standardized JSON object with all relevant information.

    Parameters
    ----------
    cindex : int
        The dscheck record index to retrieve. Default: 0
    issuer : str, optional
        Email or identifier of the person who initiated the API call.
    status_message : str, optional
        Custom status message. If provided, overrides the default generated message.
        If None or not provided, generates default message based on record status.
    request_id : str, optional
        Unique request identifier (UUID) for tracking.

    Returns
    -------
    dict
        Standardized dscheck JSON with keys:
        - request_id: Unique request identifier (if provided)
        - cindex: Record index
        - time_of_status: ISO format timestamp
        - command: Command from the dscheck record
        - argv: Arguments from the dscheck record
        - specialist: Specialist assigned to the record
        - issuer: Issuer identifier (or None if not provided)
        - status_message: Human-readable status message (custom or auto-generated)
    """
    if cindex == 0:
        response = {
            "cindex": "N/A",
            "time_of_status": datetime.now().isoformat(),
            "command": None,
            "argv": None,
            "specialist": None,
            "issuer": issuer,
            "status_message": status_message,
        }
        if request_id:
            response = {"request_id": request_id, **response}
        return response
    try:
        # Query the dscheck record
        condition = f"cindex = {cindex}"
        db = PgDBI()
        record = db.pgget("dscheck", "*", condition, db.PGLOG['LOGMASK'])

        if not record:
            response = {
                "cindex": cindex,
                "time_of_status": datetime.now().isoformat(),
                "command": None,
                "argv": None,
                "specialist": None,
                "issuer": issuer,
                "status_message": f"No dscheck record found for cindex '{cindex}'",
            }
            if request_id:
                response = {"request_id": request_id, **response}
            return response

        # Determine status message: use provided or generate default
        if status_message is None:
            # Capital word for Database columns name!!!
            record_status = record.get('STATUS', 'Unknown')
            if record_status == 'C':
                status_message = f"dscheck record for cindex '{cindex}' is queued for execution."
            elif record_status == 'R':
                status_message = f"dscheck record for cindex '{cindex}' is currently running."
            else:
                status_message = f"dscheck record for cindex '{cindex}' has status: {record_status}"

        response = {
            "cindex": cindex,
            "time_of_status": datetime.now().isoformat(),
            "command": record.get('COMMAND'),
            "argv": record.get('ARGV'),
            "specialist": record.get('SPECIALIST'),
            "issuer": issuer,
            "status_message": status_message,
            "dscheck_full_record": record
        }
        if request_id:
            response = {"request_id": request_id, **response}
        return response

    except Exception as e:
        response = {
            "cindex": cindex,
            "time_of_status": datetime.now().isoformat(),
            "command": None,
            "argv": None,
            "specialist": None,
            "issuer": issuer,
            "status_message": "Failed to retrieve dscheck record",
            "error": str(e)
        }
        if request_id:
            response = {"request_id": request_id, **response}
        return response
