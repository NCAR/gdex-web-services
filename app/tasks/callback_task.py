"""
Celery task and helpers for delivering results to a portal callback URL.

Kept apart from transform_tasks so any pipeline can reuse it: give the final
POST a task id of "{request_id}:callback-final" and GET /compose/status/{request_id}
reports it under "callback" (state in `status`, HTTP code in `info`).

Celery stores a failed task as its exception, so CallbackError carries what the
status endpoint needs in its plain args. This module must stay importable from
both the worker and the API pods.
"""
import os
import httpx
from app.celery_app import celery_app

# Custom Celery state (only PENDING/STARTED/SUCCESS/FAILURE/RETRY/REVOKED are built-ins).
# Written on "{request_id}:callback-final" when a callback_url was given, so the status
# endpoint can tell "callback requested, not sent yet" from "no callback" (PENDING).
# notify_callback overwrites it with RETRY / SUCCESS / FAILURE once it runs.
STATE_CALLBACK_WAITING = "WAITING"

# 4xx means the portal rejected the request (bad secret, bad URL, bad body), which a
# retry cannot fix; only these 4xx are worth another attempt
RETRYABLE_CALLBACK_STATUS = {408, 429}


class CallbackError(Exception):
    """The class created to represent a failure in delivering the portal callback.
    args = (message, http_status).
    
    http_status is None when the portal was never reached (connection refused, timeout).
    Kept as plain args so Celery can rebuild it from the result backend and
    GET /compose/status can read the status code from the task's info.
    """


def mark_callback_waiting(request_id: str) -> None:
    """Record that a callback is expected for request_id, before anything can finish and fire it."""
    celery_app.backend.store_result(f"{request_id}:callback-final", None, STATE_CALLBACK_WAITING)


@celery_app.task(bind=True, max_retries=5, default_retry_delay=10)
def notify_callback(self, result: dict, callback_url: str) -> dict:
    """POST the final job result to the portal's callback URL.

    Deliberately separate from watch_job: a portal endpoint gets its
    own retry/backoff and doesn't affect tracking of the PBS job itself.
    Delivery is at-least-once, so the receiver should treat repeats for the
    same request_id as idempotent.

    Outcome, as read from AsyncResult("{request_id}:callback-final"):
      SUCCESS  info = {"http_status": <2xx code>}
      FAILURE  info = CallbackError(message, http_status): at once for a rejection (4xx
               except 408/429), or after max_retries for 5xx/408/429/network errors
      RETRY    info = the CallbackError of the latest failed attempt
    """

    shared_secret = os.environ["GDEX_SERVICES_CALLBACK_SECRET"]
    delay = self.default_retry_delay * 2 ** self.request.retries
    try:
        resp = httpx.post(
            callback_url,
            json=result,
            headers={
                "Content-Type": "application/json",
                "X-GDEX-Services-Secret": shared_secret
            },
            timeout=10
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        err = CallbackError(f"Portal callback returned HTTP {status}", status)
        if status < 500 and status not in RETRYABLE_CALLBACK_STATUS:
            raise err from exc
        raise self.retry(exc=err, countdown=delay)
    # connection refused, DNS failure, timeout: retry with exponential backoff
    except httpx.HTTPError as exc:
        raise self.retry(exc=CallbackError(f"Portal callback not reached: {exc!r}", None), countdown=delay)
    return {"http_status": resp.status_code}
