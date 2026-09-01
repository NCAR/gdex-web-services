"""Utilities for generating and uploading transformation payloads to Boreas."""

import json
from typing import Dict, Any

from app.schemas.models import TransformRequest
from app.utils.boreas import _OBJECT_STORE_ENDPOINT, s3_client

_BUCKET = "gdex-data"
_PAYLOAD_PREFIX = "services_tmp/payloads"


def _serialize_transform_request(request: TransformRequest) -> Dict[str, Any]:
    """Convert TransformRequest to JSON-serializable payload format.

    Parameters
    ----------
    request : TransformRequest
        The transformation request object.

    Returns
    -------
    dict
        JSON-serializable dictionary with "Files" and "Commands" keys.
    """
    return {
        "Files": request.files,
        "Commands": [cmd.model_dump() for cmd in request.commands]
    }


def _upload_payload(
    payload_dict: Dict[str, Any],
    filename: str = "transform.payload.json",
    prefix: str = _PAYLOAD_PREFIX
) -> str:
    """Upload payload dictionary to Boreas object store.

    Parameters
    ----------
    payload_dict : dict
        The payload dictionary to upload.
    filename : str, optional
        Name of the file in the object store. Default: "transform.payload.json"
    prefix : str, optional
        S3 prefix/directory for payload storage. Default: "services_tmp/payloads"

    Returns
    -------
    str
        Public HTTPS URL to the uploaded payload.
    """
    payload_json = json.dumps(payload_dict, indent=2)

    key = f"{prefix}/{filename}"
    s3_client().put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=payload_json.encode("utf-8"),
        ContentType="application/json",
        ACL="public-read",
    )

    return f"{_OBJECT_STORE_ENDPOINT}/{_BUCKET}/{key}"


def create_transform_payload(
    request: TransformRequest,
    filename: str = "transform.payload.json",
    prefix: str = _PAYLOAD_PREFIX
) -> str:
    """Serialize TransformRequest and upload to Boreas object store.

    Parameters
    ----------
    request : TransformRequest
        The transformation request object to upload.
    filename : str, optional
        Name of the file in the object store. Default: "transform.payload.json"
    prefix : str, optional
        S3 prefix/directory for payload storage. Default: "services_tmp/payloads"

    Returns
    -------
    str
        Public HTTPS URL to the uploaded payload.
    """
    payload_dict = _serialize_transform_request(request)
    return _upload_payload(payload_dict, filename=filename, prefix=prefix)
