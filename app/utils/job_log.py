"""Utilities for uploading a transform job's JSONL log to Boreas."""

from app.utils.boreas import _OBJECT_STORE_ENDPOINT, s3_client

_BUCKET = "gdex-services"
_LOG_PREFIX = "services_tmp/logs"


def upload_job_log(content: bytes, request_id: str, prefix: str = _LOG_PREFIX) -> str:
    """Upload the job log to Boreas, byte for byte.

    Parameters
    ----------
    content : bytes
        The log file content, uploaded as is.
    request_id : str
        External job id; the object is named "transform.{request_id}.jsonl".
    prefix : str, optional
        S3 prefix/directory for logs. Default: "services_tmp/logs"

    Returns
    -------
    str
        Public HTTPS URL to the uploaded log.
    """
    key = f"{prefix}/transform.{request_id}.jsonl"
    s3_client().put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=content,
        ContentType="application/x-ndjson",
        ACL="public-read",
    )

    return f"{_OBJECT_STORE_ENDPOINT}/{_BUCKET}/{key}"
