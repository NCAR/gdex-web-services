import os

import boto3

_OBJECT_STORE_ENDPOINT = "https://boreas.hpc.ucar.edu"


def s3_client():
    """Build a boto3 S3 client for the boreas object store using credentials from the environment."""
    return boto3.client(
        "s3",
        endpoint_url=_OBJECT_STORE_ENDPOINT,
        aws_access_key_id=os.environ["OBJECT_STORE_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["OBJECT_STORE_SECRET_KEY"],
    )
