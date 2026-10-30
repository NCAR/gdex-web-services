"""Utility function for sending task results to a caller's callback URL.

The gdex-web-portal's GdexServicesReceiver only accepts callbacks carrying the
shared secret from the gdex-services-callback-secret Kubernetes Secret, which
the Celery worker receives as the GDEX_SERVICES_CALLBACK_SECRET env var.

Both callback paths (portal jobs in app/tasks/portal/common.py and transform jobs
in app/tasks/callback_task.py) send through here; each decides how to handle a failure.
"""

import os
import httpx


def callback_to_portal(url, payload, timeout=10):
    """POST a JSON payload to a callback URL with the gdex-services shared secret.

    Sends the secret in the X-GDEX-Services-Secret header, read from the
    GDEX_SERVICES_CALLBACK_SECRET env var on every call. Raises KeyError if the
    env var is unset, rather than sending a callback the portal would reject.
    Nothing is retried here; callers choose their own retry policy.

    Parameters
    ----------
    url : str
        Callback URL to POST to. The secret is attached whatever the host,
        so only pass URLs that should receive it.
    payload : dict
        JSON-serializable body, e.g. a portal.common.job_response() dict.
    timeout : float
        Seconds to wait for the portal. Default: 10

    Returns
    -------
    httpx.Response
        The portal's 2xx response.

    Raises
    ------
    httpx.HTTPStatusError
        If the portal answers with a 4xx/5xx status (e.response has the details).
    httpx.HTTPError
        If the portal can't be reached (connection error, timeout).
    KeyError
        If GDEX_SERVICES_CALLBACK_SECRET is not set.
    """

    # Only these portal endpoints may receive the shared secret.
    ALLOWED_CALLBACK_URLS = frozenset({
        "https://gdex.ucar.edu/api/gdex-services-event/",
        "https://calie.k8s.ucar.edu/api/gdex-services-event/",
    })

    if url not in ALLOWED_CALLBACK_URLS:
        raise ValueError(f"Callback URL {url} is not allowed for portal callbacks.")

    shared_secret = os.environ["GDEX_SERVICES_CALLBACK_SECRET"]
    response = httpx.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "X-GDEX-Services-Secret": shared_secret
        },
        timeout=timeout
    )
    response.raise_for_status()
    return response
