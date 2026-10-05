"""
Celery tasks for the POST /compose/transform pipeline.

These wrap the existing utils (payload/PBS upload, dscheck inserts) so the
work runs on the celery worker Deployment instead of the API request thread.

Pipeline (started by celery_transform task):

    create_boreas_artifacts -> add_dscheck_download -> add_dscheck_qsub

plus, in parallel, watch_job (task_id == request_id), which tails
{request_id}.gdexws.jsonl and mirrors it into Celery state for
GET /compose/status/{request_id}, then fires notify_callback if a
callback URL was supplied.
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from celery import chain
from celery.exceptions import Ignore

from rda_python_common.pg_dbi import PgDBI
from rda_python_common.pg_log import PgLOG

from app.celery_app import celery_app
from app.schemas.models import TransformRequest
from app.utils import create_transform_payload, create_pbs_script

WORKDIR = str(Path("/glade/campaign/collections/gdex/data/exchange/Web-services/"))

# Terminal process_message values echoed by the PBS script (app/utils/pbs.py)
PBS_COMPLETED = "PBS job completed"
PBS_FAILED = "PBS job failed"

# Custom Celery states. Only PENDING/STARTED/SUCCESS/FAILURE/RETRY/REVOKED are
# Celery built-ins; anything else means "still going, check info/meta".
STATE_PBS_PROGRESS = "PBS_PROGRESS"
STATE_PBS_FAILURE_REPORTED = "PBS_FAILURE_REPORTED"

READ_JSONL_INT = 15        # seconds between JSONL reads once the log exists
SEARCH_JSONL_INT = 10        # seconds between checks while the log does not exist yet

# pgadd/pgget log flags: log errors but never EXITLG — sys.exit(1) would kill the
# worker process (WorkerLostError) instead of failing/retrying the task normally.
DSCHECK_LOGACT = PgLOG.LOGERR


def _sql_str(value: str) -> str:
    """Quote a value as a SQL string literal (pgget takes a raw condition string)."""
    return "'" + value.replace("'", "''") + "'"


def _add_dscheck_row(record: dict) -> int:
    """Insert a dscheck row, or return the cindex of the one already there.

    dscheck is unique on (command, argv), and argv embeds request_id, so a
    rejected insert means this request's row was added by an earlier run of
    the same task (e.g. redelivered after a worker died before acking). Reusing
    that row's cindex makes the insert safe to repeat.

    Returns 0 if the insert failed and no matching row exists.
    """
    db = PgDBI()
    cindex = db.pgadd("dscheck", record, DSCHECK_LOGACT | PgLOG.AUTOID | PgLOG.DODFLT)
    if cindex and cindex > 0:
        return cindex
    condition = f"command = {_sql_str(record['command'])} AND argv = {_sql_str(record['argv'])}"
    existing = db.pgget("dscheck", "cindex", condition, DSCHECK_LOGACT)
    return existing.get("cindex", 0) if existing else 0


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def create_boreas_artifacts(self, request_dict: dict, request_id: str) -> dict:
    """Upload payload.json and the PBS script to Boreas.

    Idempotent: re-running just re-uploads to the same S3 key, so a retry
    after a crash is safe.
    """
    request = TransformRequest(**request_dict)
    try:
        #generate payload url first to put in the PBS script
        payload_url = create_transform_payload(request, request_id=request_id)
        pbs_url = create_pbs_script(payload_url, request_id=request_id)
    except Exception as exc:
        raise self.retry(exc=exc) # handle exception and retry
    return {"request_id": request_id, "payload_url": payload_url, "pbs_url": pbs_url}


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def add_dscheck_download(self, artifacts: dict, specialist: str) -> dict:
    """Insert the dscheck row that tells the HPC daemon to curl the PBS script.

    Safe to redeliver: if this request's curl row already exists, its cindex
    is reused and the chain continues (see _add_dscheck_row).
    """
    request_id = artifacts["request_id"]
    dict_dscheck_post = {
        "command": "curl",
        "specialist": specialist,
        # Download PBS script and save locally as: transform.{request_id}.pbs
        "argv": f'-o transform.{request_id}.pbs "$DOWNLOAD_URL"',
        "environments": f"DOWNLOAD_URL={artifacts['pbs_url']}",
        "workdir": WORKDIR,
    }
    cindex_download = _add_dscheck_row(dict_dscheck_post)
    if cindex_download <= 0:
        raise self.retry(exc=RuntimeError("dscheck returned invalid cindex for PBS download"))
    return {**artifacts, "specialist": specialist, "cindex_download": cindex_download}


@celery_app.task(bind=True, max_retries=18, default_retry_delay=10)
def add_dscheck_qsub(self, prev: dict) -> dict:
    """Wait for the downloaded PBS script, then insert the qsub dscheck row.

    Same ~3-minute timeout (18 retries * 10s), but each wait is a re-queue rather than a worker
    sleeping, and it survives a worker restart. Safe to redeliver, same as
    add_dscheck_download.
    """

    # retry till the PBS script is downloaded
    request_id = prev["request_id"]
    pbs_script_path = Path(WORKDIR) / f"transform.{request_id}.pbs"
    if not pbs_script_path.exists():
        raise self.retry(exc=RuntimeError(f"PBS script not downloaded yet at {pbs_script_path}"))

    # REQUEST_ID is passed as env var to PBS script for JSONL filename
    dict_dscheck_post = {
        "command": "qsub",
        "specialist": prev["specialist"],
        "argv": f"-v REQUEST_ID transform.{request_id}.pbs",
        "environments": f"REQUEST_ID={request_id}",
        "workdir": WORKDIR,
    }
    cindex_submit = _add_dscheck_row(dict_dscheck_post)
    if cindex_submit <= 0:
        raise self.retry(exc=RuntimeError("dscheck returned invalid cindex for qsub"))
    return {**prev, "cindex_submit": cindex_submit}


def jsonl_to_celery_state(entry: Dict[str, Any]) -> str:
    """Turn JSONL line (gdexws log_format dict) into a Celery state."""
    if entry.get("level") == "ERROR":
        # "the job reported an error" — not a Celery-level failure but PBS level failure
        return STATE_PBS_FAILURE_REPORTED
    return STATE_PBS_PROGRESS


def _read_complete_jsons(log_path: Path) -> list:
    """Return only newline-terminated lines, so a line the PBS job is still
    appending is picked up on the next poll instead of failing json.loads."""
    text = log_path.read_text()
    lines = text.split("\n")
    # split() leaves the trailing fragment (or "" if text ends with "\n") last
    return lines[:-1]


@celery_app.task(bind=True, max_retries=5, default_retry_delay=10)
def notify_callback(self, result: dict, callback_url: str) -> None:
    """POST the final job result to the portal's callback URL.

    Deliberately separate from watch_job: a portal endpoint gets its
    own retry/backoff and doesn't affect tracking of the PBS job itself.
    Delivery is at-least-once, so the receiver should treat repeats for the
    same request_id as idempotent.
    """

    try:
        resp = httpx.post(callback_url, json=result, timeout=10)
        resp.raise_for_status()
    # if the HTTP POST fails, retry with exponential backoff
    except httpx.HTTPError as exc:
        raise self.retry(exc=exc, countdown=self.default_retry_delay * 2 ** self.request.retries)


@celery_app.task(bind=True, track_started=False)
def watch_job(self, request_id: str, callback_url: Optional[str] = None, last_line: int = 0):
    """Tail {request_id}.gdexws.jsonl and mirror it into this task's state.

    Each run is one jsonl file read: if the job hasn't finished, the task
    re-schedules itself under the SAME task id and exits, so a worker slot
    is never held for the whole PBS wait+execution. The re-schedule is done with
    apply_async + Ignore rather than self.retry(), because retry() would
    overwrite the PROGRESS state/meta with RETRY on every poll.
    track_started is off for the same reason (STARTED would clobber meta).

    States seen by GET /compose/status/{request_id}:
      PENDING           log file not written yet (or unknown request_id)
      PROGRESS          latest JSONL line, level != ERROR (info = that line)
      PBS_FAILURE_REPORTED  latest JSONL line, level == ERROR (info = that line), PBS job error reported
      SUCCESS           terminal line seen; info.job_status = completed|failed
      FAILURE           the celery worker/watcher itself broke (bad JSON, I/O error, bug)

    Independently invokable: nothing here requires having come from
    POST /compose/transform. For a job re-run by hand on HPC:
        watch_job.apply_async(args=[request_id, callback_url], task_id=request_id)
    The PBS script and gdexws CLI never need to know this task, or a
    callback, exists.
    """
    log_path = Path(WORKDIR) / f"{request_id}.gdexws.jsonl"

    # if log does not exist
    if not log_path.exists():
        # change state to PENDING
        self.update_state(state="PENDING", meta={"info": "log file not written yet"})
        # reschedule the watch to check again later
        _reschedule_watch(self, request_id, callback_url, last_line, SEARCH_JSONL_INT)

    lines = _read_complete_jsons(log_path)
    new_entries = [json.loads(line) for line in lines[last_line:] if line.strip()]
    new_last_line = len(lines)

    # if no new entries, just reschedule the watch without changing state
    if not new_entries:
        _reschedule_watch(self, request_id, callback_url, last_line, READ_JSONL_INT)

    for entry in new_entries:
        msg = entry.get("process_message")
        if msg in (PBS_COMPLETED, PBS_FAILED):
            # Terminal line: the PBS job has finished, successfully or not
            if msg == PBS_COMPLETED:
                job_status = "completed"
            else:
                job_status = "failed"

            result = {"request_id": request_id, "job_status": job_status}
            result.update(entry)  # add the JSONL line itself (command, time_of_process, level, ...)

            if callback_url:  # only if one was provided in the payload
                notify_callback.delay(result, callback_url)
            return result

    # only update the state with the latest entry if there are new entries
    latest = new_entries[-1]
    self.update_state(state=jsonl_to_celery_state(latest), meta=latest)

    _reschedule_watch(self, request_id, callback_url, new_last_line, READ_JSONL_INT)


def _reschedule_watch(task, request_id: str, callback_url: Optional[str], last_line: int, countdown: int):
    """Queue the next watch_job poll (a manual retry) under the same task id, then stop this
    run without writing a result (Ignore keeps the current state/meta)."""
    watch_job.apply_async(
        args=[request_id, callback_url, last_line],
        task_id=task.request.id,
        countdown=countdown,
    )
    raise Ignore()


def celery_transform(request_dict: dict, request_id: str, specialist: str) -> None:
    """Called from the router. Fires the build -> curl -> qsub chain and starts
    the watcher under task_id=request_id, so AsyncResult(request_id) is the
    status lookup and no request_id <-> task_id mapping is needed.

    request_dict is TransformRequest.model_dump(by_alias=True).
    callback_url only ever travels to watch_job — the chain, the dscheck rows
    and the PBS script never see it.
    """
    chain(
        create_boreas_artifacts.s(request_dict, request_id),
        add_dscheck_download.s(specialist), # the first arg is the return of the previous task
        add_dscheck_qsub.s(),
    ).apply_async()

    # initialize the watch job for this request
    # (the watcher will keep retrying until the PBS job finishes or fails)
    watch_job.apply_async(
        args=[request_id, request_dict.get("CallbackUrl")],
        task_id=request_id,
    )
