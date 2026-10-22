"""Utilities for writing a portal job's log as JSONL and saving it to Boreas.

Lines use the same format as the transform jobs' {request_id}.gdexws.jsonl
(gdexws.utils.logging.log_format), so both kinds of job log read the same:

    {"command": "...", "time_of_process": "...", "level": "...", "process_message": "...", ...}

Unlike transform, nothing is written to /glade: the log lives only on Boreas.
"""

import json
from datetime import datetime
from typing import Any, Dict, List

from app.utils.boreas import _OBJECT_STORE_ENDPOINT, s3_client

_BUCKET = "gdex-data"
_LOG_PREFIX = "services_tmp/logs"

_VALID_LEVELS = {"ERROR", "WARNING", "INFO", "DEBUG"}


def log_format(command_name: str, level: str, process_message: str, **kwargs) -> Dict[str, Any]:
    """Create a structured log dictionary in the gdexws JSONL format.

    Copied from gdexws.utils.logging.log_format, which isn't installed in the
    worker image; keep the two in step.

    Parameters
    ----------
    command_name : str
        Name of the command or step being executed.
    level : str
        Log level (ERROR, WARNING, INFO, DEBUG).
    process_message : str
        Free format message for status tracking.
    **kwargs
        Additional key=value pairs to include in the dict.

    Returns
    -------
    dict
        {"command": ..., "time_of_process": ..., "level": ..., "process_message": ..., **kwargs}

    Raises
    ------
    ValueError
        If level is not ERROR, WARNING, INFO, or DEBUG.
    """
    if level not in _VALID_LEVELS:
        raise ValueError(f"level must be one of {_VALID_LEVELS}, got '{level}'")

    log_dict = {
        "command": command_name,
        "time_of_process": datetime.now().isoformat(),
        "level": level,
        "process_message": process_message,
    }
    log_dict.update(kwargs)
    return log_dict


def _job_log_key(endpoint: str, job_id: str, prefix: str = _LOG_PREFIX) -> str:
    """S3 key of a job's log: <prefix>/<endpoint>.<job_id>.jsonl.

    The endpoint name is standardized for the filename: any leading "/" is
    dropped and "-" becomes "_", so "/check-data" gives check_data.<job_id>.jsonl.
    """
    name = endpoint.strip("/").replace("-", "_")
    return f"{prefix}/{name}.{job_id}.jsonl"


def job_log_url(endpoint: str, job_id: str, prefix: str = _LOG_PREFIX) -> str:
    """Public HTTPS URL of a job's log on Boreas (whether or not it's been uploaded yet).

    Parameters
    ----------
    endpoint : str
        Name of the endpoint that started the job, e.g. "check-data"; written
        into the filename with "_" in place of "-" (check_data).
    job_id : str
        The job's external_job_id, used in the filename.
    prefix : str, optional
        S3 prefix/directory for logs. Default: "services_tmp/logs"

    Returns
    -------
    str
        e.g. https://boreas.hpc.ucar.edu/gdex-data/services_tmp/logs/check_data.<job_id>.jsonl
    """
    return f"{_OBJECT_STORE_ENDPOINT}/{_BUCKET}/{_job_log_key(endpoint, job_id, prefix)}"


def upload_job_log(endpoint: str, job_id: str, lines: List[Dict[str, Any]], prefix: str = _LOG_PREFIX) -> str:
    """Upload a job's log lines to Boreas as JSONL, replacing any earlier upload.

    Saved as <prefix>/<endpoint>.<job_id>.jsonl, e.g.
    services_tmp/logs/check_data.<job_id>.jsonl (see _job_log_key).

    Parameters
    ----------
    endpoint : str
        Name of the endpoint that started the job, e.g. "check-data"; written
        into the filename with "_" in place of "-" (check_data).
    job_id : str
        The job's external_job_id, used in the filename.
    lines : list of dict
        Log lines from log_format, written one JSON object per line.
    prefix : str, optional
        S3 prefix/directory for logs. Default: "services_tmp/logs"

    Returns
    -------
    str
        Public HTTPS URL to the uploaded log (same as job_log_url).
    """
    body = "".join(json.dumps(line) + "\n" for line in lines)
    s3_client().put_object(
        Bucket=_BUCKET,
        Key=_job_log_key(endpoint, job_id, prefix),
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
        ACL="public-read",
    )
    return job_log_url(endpoint, job_id, prefix)
