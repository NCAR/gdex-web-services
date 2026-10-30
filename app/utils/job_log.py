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

_BUCKET = "gdex-services"
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


def upload_job_log(endpoint: str, external_job_id: str, json_lines: List[Dict[str, Any]], prefix: str = _LOG_PREFIX) -> str:
    """Upload a job's log lines to Boreas as JSONL, replacing any earlier upload.

    Key of the object : <prefix>/<endpoint>.<external_job_id>.jsonl
    e.g.
        services_tmp/logs/check_data.<external_job_id>.jsonl

    (any leading "/" on the endpoint is dropped and "-" becomes "_".)

    Parameters
    ----------
    endpoint : str
        Name of the endpoint that started the job, e.g. "check-data"; written
        into the filename with "_" in place of "-" (check_data).
    external_job_id : str
        The job's external_job_id, used in the filename.
    json_lines : list of dict
        Log lines from log_format, written one JSON object per line.
    prefix : str, optional
        S3 prefix/directory for logs. Default: "services_tmp/logs"

    Returns
    -------
    str
        Public HTTPS URL to the uploaded log, e.g.
        https://boreas.hpc.ucar.edu/gdex-services/services_tmp/logs/check_data.<external_job_id>.jsonl
    """
    # create the S3 key for the log file
    key = f"{prefix}/{endpoint.strip('/').replace('-', '_')}.{external_job_id}.jsonl"
    # turn list of jsons to jsonl
    body = "".join(json.dumps(jline) + "\n" for jline in json_lines)
    # put object
    s3_client().put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
        ACL="public-read",
    )
    return f"{_OBJECT_STORE_ENDPOINT}/{_BUCKET}/{key}"
