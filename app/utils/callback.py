"""Utility functions for sending task results to a caller's callback URL.

The gdex-web-portal's GdexServicesReceiver only accepts callbacks carrying the
shared secret from the gdex-services-callback-secret Kubernetes Secret, which
the Celery worker receives as the GDEX_SERVICES_CALLBACK_SECRET env var.
"""

import os

import requests


def post_to_callback_url(url, payload):
    """POST a JSON payload to a callback URL with the gdex-services shared secret.

    Sends the secret in the X-GDEX-Services-Secret header, read from the
    GDEX_SERVICES_CALLBACK_SECRET env var on every call. If the env var is
    unset, an empty header is sent and the portal will reject the callback.

    The outcome is printed to the worker log rather than raised: a rejected
    callback logs the status code and the portal's JSON "message" (or the
    first 500 characters of a non-JSON body). Nothing is retried.

    Parameters
    ----------
    url : str
        Callback URL to POST to. The secret is attached whatever the host,
        so only pass URLs that should receive it.
    payload : dict
        JSON-serializable body, e.g. a portal.common.job_response() dict.

    Returns
    -------
    None

    Raises
    ------
    requests.RequestException
        If the request can't be sent (e.g. connection error).
    ValueError
        If the callback succeeds but its response body isn't JSON.
    """
    # The portal rejects callbacks without its shared secret (gdex-services-callback-secret)
    headers = {"X-GDEX-Services-Secret": os.environ.get("GDEX_SERVICES_CALLBACK_SECRET", "")}
    response = requests.post(url, json=payload, headers=headers)
    if response.ok:
        print("Success")
        print(response.json())
    else:
        # The portal explains rejections in a JSON "message" (e.g. secret missing or
        # mismatched, unknown task id); fall back to the raw body for non-JSON errors
        try:
            reason = response.json().get("message", response.text)
        except (ValueError, AttributeError):
            reason = response.text[:500]
        print(f"Callback to {url} failed with status code {response.status_code}: {reason}")
