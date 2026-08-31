"""Utility functions for dscheck record formatting and retrieval."""

import json
import subprocess
from datetime import datetime
from typing import Dict, Any, Optional

try:
    from rda_python_common.pg_dbi import PgDBI
except ImportError as e:
    raise RuntimeError(
        f"Failed to import rda_python_common: {e}\n"
        "Install with: pip install rda-python-common"
    ) from e


def _read_latest_log(cindex: int) -> Optional[Dict[str, Any]]:
    """Read the latest log entry from the JSONL file for a given cindex.

    Parameters
    ----------
    cindex : int
        The dscheck record index to read logs for.

    Returns
    -------
    dict or None
        The parsed JSON object from the last line of the JSONL file,
        or None if the file cannot be read or parsed.
    """
    log_path = f"/glade/campaign/collections/gdex/data/exchange/Web-services/{cindex}.gdexws.jsonl"

    try:
        result = subprocess.run(
            ["tail", "-1", log_path],
            capture_output=True,
            text=True,
            timeout=5,
            check=True
        )
        if result.returncode == 0:
            return json.loads(result.stdout.strip())
        return None
    except json.JSONDecodeError:
        return None
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def get_dscheck_json(cindex: int = 0, issuer: Optional[str] = None, status_message: str = "") -> Dict[str, Any]:
    """Retrieve and format a dscheck record with its latest processing status.

    Queries the dscheck database table for the given cindex and retrieves
    the latest log entry from the corresponding JSONL file. Returns a
    standardized JSON object with all relevant information.

    Parameters
    ----------
    cindex : int
        The dscheck record index to retrieve.
    issuer : str, optional
        Email or identifier of the person who initiated the API call.

    Returns
    -------
    dict
        Standardized dscheck JSON with keys:
        - cindex: Record index
        - time_of_status: ISO format timestamp
        - command: Command from the dscheck record
        - argv: Arguments from the dscheck record
        - specialist: Specialist assigned to the record
        - issuer: Issuer identifier (or None if not provided)
        - status_message: Human-readable status message
        - processing_status: Latest log entry from JSONL file (or None)
    """
    if cindex == 0:
        return {
            "cindex": "N/A",
            "time_of_status": datetime.now().isoformat(),
            "command": None,
            "argv": None,
            "specialist": None,
            "issuer": issuer,
            "status_message": status_message,
            "processing_status": None
        }
    try:
        # Query the dscheck record
        condition = f"cindex = {cindex}"
        db = PgDBI()
        record = db.pgget("dscheck", "*", condition, db.PGLOG['LOGMASK'])

        if not record:
            return {
                "cindex": cindex,
                "time_of_status": datetime.now().isoformat(),
                "command": None,
                "argv": None,
                "specialist": None,
                "issuer": issuer,
                "status_message": f"No dscheck record found for cindex '{cindex}'",
                "processing_status": None
            }

        # Get the processing status from the latest log
        processing_status = _read_latest_log(cindex)

        # Determine status message based on record status
        # Captial word for Database columns name!!!
        record_status = record.get('STATUS', 'Unknown')
        if record_status == 'C':
            status_message = f"dscheck record for cindex '{cindex}' is queued for execution."
        elif record_status == 'R':
            status_message = f"dscheck record for cindex '{cindex}' is currently running."
        else:
            status_message = f"dscheck record for cindex '{cindex}' has status: {record_status}"

        return {
            "cindex": cindex,
            "time_of_status": datetime.now().isoformat(),
            "command": record.get('COMMAND'),
            "argv": record.get('ARGV'),
            "specialist": record.get('SPECIALIST'),
            "issuer": issuer,
            "status_message": status_message,
            "processing_status": processing_status,
            "dscheck_full_record": record
        }

    except Exception as e:
        return {
            "cindex": cindex,
            "time_of_status": datetime.now().isoformat(),
            "command": None,
            "argv": None,
            "specialist": None,
            "issuer": issuer,
            "status_message": "Failed to retrieve dscheck record",
            "processing_status": None,
            "error": str(e)
        }
