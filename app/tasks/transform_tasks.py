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
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from celery import chain
from celery.result import AsyncResult
from celery.exceptions import Ignore

from rda_python_common.pg_dbi import PgDBI
from rda_python_common.pg_log import PgLOG

from app.celery_app import celery_app
from app.schemas.models import TransformRequest
from app.tasks.callback_task import notify_callback, mark_callback_waiting
from app.tasks.portal.common import job_response
from app.utils import create_transform_payload, create_pbs_script, upload_job_log

logger = logging.getLogger(__name__)

# Tracked tasks run under task_id "{request_id}:{name}" (watch_job keeps the bare request_id).
# GET /compose/status/{request_id} reports the state of each of these.
STEP_NAMES = ["artifacts", "download", "qsub", "callback-final"]

WORKDIR = str(Path("/glade/campaign/collections/gdex/data/exchange/Web-services/"))

# Terminal process_message values echoed by the PBS script (app/utils/pbs.py)
PBS_COMPLETED = "PBS job completed"
PBS_FAILED = "PBS job failed"

# Custom Celery states. 
# Only PENDING/STARTED/SUCCESS/FAILURE/RETRY/REVOKED are Celery built-ins.
STATE_PBS_PROGRESS = "PBS_PROGRESS"
STATE_PBS_FAILURE_REPORTED = "PBS_FAILURE_REPORTED"

READ_JSONL_INT = 15        # seconds between JSONL reads once the log exists
SEARCH_JSONL_INT = 10        # seconds between checks while the log does not exist yet
# Silent polls never write to Redis, so the result key's TTL (result_expires) would run out
# on a long quiet job. Re-save the current state this often; keep it well under result_expires.
HEARTBEAT_INT = 12 * 60 * 60  # seconds

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

def _check_dscheck_row(cindex: int) -> int:
    """Check if a dscheck row exists for the given command and argv."""
    db = PgDBI()
    condition = f"cindex = {cindex}"
    existing = db.pgget("dscheck", "status", condition, DSCHECK_LOGACT)
    # pgget returns an empty result (not a dict with the key) when no row matches
    return existing.get("status") if existing else None


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
        # and not the final completion of the job that show "PBS job failed"
        # this is catching the ERROR line produced by the service_log in gdexws cli
        return STATE_PBS_FAILURE_REPORTED
    return STATE_PBS_PROGRESS


def _read_complete_jsons(log_path: Path) -> tuple:
    """Return (entries, raw): the parsed newline-terminated lines of the log, one slot per
    line, and the exact bytes they were parsed from (for a byte-identical upload).

    A line the PBS job is still appending (no trailing newline) is left for the
    next poll. A blank line comes back as {"json_msg": "empty line"} and an undecodable one as
    {"json_msg": "json decode fail at line N"}, so the list length still equals the
    number of complete lines (the caller's last_line index); a corrupt line never
    heals, so it is reported once and not retried."""
    data = log_path.read_bytes()
    raw = data[:data.rfind(b"\n") + 1]  # up to and including the last newline
    # raw ends with "\n" (or is empty), so split() leaves "" last
    lines = raw.decode("utf-8", errors="replace").split("\n")[:-1]
    entries = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            entries.append({"json_msg": "empty line"})
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"json_msg": f"json decode fail at line {number}"})
    return entries, raw


@celery_app.task
def chain_failed(request, exc, traceback, request_id: str, callback_url: Optional[str] = None) -> None:
    """Error handler (link_error) for the build -> curl -> qsub chain.

    Runs when a chain step fails for good (after its retries). Celery already
    skips the steps after the failed one; this marks the request FAILURE on the
    request_id key (what GET /compose/status/{request_id} reads, and what makes
    watch_job stop polling) and sends the final callback so the portal is told.
    The failing step is the suffix of its task id ("{request_id}:{step}").

    A duplicate run of the chain (redelivered message) can fail after another run already
    got the job going. Only act while the request is still PENDING (watch_job has seen no
    log yet); any other state means the job is tracked, finished, or already failed.
    """
    step = request.id.rsplit(":", 1)[-1]
    current = AsyncResult(request_id, app=celery_app).state
    if current != "PENDING":
        logger.warning(
            f"Ignoring failure of step '{step}' for request_id={request_id}: "
            f"request already in state {current} (duplicate chain run?)"
        )
        return
    msg = f"Step '{step}' failed: {exc!r}"
    celery_app.backend.mark_as_failure(request_id, RuntimeError(msg), traceback=traceback)

    if callback_url:
        call_back_json = job_response(
            task_id=request_id, status="FAILURE", result={"step": step, "msg": msg},
            job_type="transform", completed=datetime.now(timezone.utc).isoformat(),
        )
        notify_callback.apply_async(
            args=[call_back_json,callback_url],
            task_id=f"{request_id}:callback-final",
        )


@celery_app.task(bind=True, track_started=False)
def watch_job(self, request_id: str, callback_url: Optional[str] = None, last_line: int = 0,
              last_refresh: Optional[float] = None):
    """Tail {request_id}.gdexws.jsonl and mirror it into this task's state.

    Each run is one jsonl file read: if the job hasn't finished, the task
    re-schedules itself under the SAME task id and exits, so a worker slot
    is never held for the whole PBS wait+execution.
    
    Implementation details:
        The re-schedule is done with
        apply_async + Ignore rather than self.retry(), because retry() would
        overwrite the PROGRESS state/meta with RETRY on every poll.
        track_started is off for the same reason (STARTED would clobber meta).

    States seen by GET /compose/status/{request_id}:
      PENDING           log file not written yet (or unknown request_id)
      PBS_PROGRESS          latest JSONL line, level != ERROR (info/meta = json line)
      PBS_FAILURE_REPORTED  latest JSONL line, level == ERROR (info/meta = json ERROR line), PBS job error reported
      SUCCESS           terminal line seen
      FAILURE           the celery worker/watcher itself broke (bad JSON, I/O error, bug),
                        or a chain step failed (set by chain_failed; polling then stops)

    Detailed celery worker meta information
    1. if log file not exist 
       - state = PENDING
       - meta.msg = "log file not written yet"
    2. if new entries exist but none are terminal
       - state = PBS_PROGRESS
       - meta.msg = latest JSONL line
    3. if new entries exist but none are terminal WITH error in json in jsonl with "level" : "ERROR"
       - state = PBS_FAILURE_REPORTED
       - meta.msg = latest JSONL line with "level" : "ERROR"
    4. if a terminal line is found (i.e. PBS_COMPLETED or PBS_FAILED)
       - state = SUCCESS|PBS_FAILURE_REPORTED
       - meta.msg = where the log file is, e.g. "Completed successfully/Failed. Check the {log_path} for details."


    Possible task handling issues:
    1. The task keeps rescheduling if the PBS job never writes a terminal line, potentially leading to an infinite loop.
        Solution: check if the PBS job still exists in the watcher before rescheduling.
    2. The task may never notify the portal if it crashes due to an unhandled exception (bad JSON line, I/O error, bug).
        Solution: 
            1. JSON decode errors handled in json parsing `_read_complete_jsons()`
            2. task other error is handled by the try-except blocks surrounding the main logic which return 503 HTTP status code
            3. chain failures are handled separately by the `chain_failed` function, which marks the request as FAILURE and notifies the callback.
            4. watcher failures are handled by the try-except blocks within the watcher task itself
            ensuring that unexpected exceptions are caught and the task is marked as FAILURE appropriately.
    3. The task is taking a long time for each update if the PBS job is slow to write new entries, 
       potentially causing task result is removed due to timeout or other resource constraints.
        Solution: The remove of task result in redis is one day. A half-day heartbeat interval ensures 
        that the task result is refreshed periodically, preventing it from being removed prematurely.
        set HEARTBEAT_INT to half a day (e.g., 12 * 60 * 60 seconds) to ensure periodic refresh of the task result.

    """
    # Define jsonl log path
    log_path = Path(WORKDIR) / f"{request_id}.gdexws.jsonl"

    # a chain step failed: chain_failed already marked this request FAILURE and notified
    # the callback, so stop polling and keep that state
    if self.AsyncResult(request_id).state == "FAILURE":
        raise Ignore()

    # cindex_submit (the dscheck row of the qsub step) is kept in this task's meta so it
    # outlives the "{request_id}:qsub" result key; every update_state below carries it via _meta()
    prev_result = self.AsyncResult(request_id)
    prev_meta = prev_result.info
    cindex_submit = prev_meta.get("cindex_submit") if isinstance(prev_meta, dict) else None

    def _meta(meta: dict) -> dict:
        return {**meta, "cindex_submit": cindex_submit} if cindex_submit is not None else meta

    # Check the PBS job still exists before looking at the log. This runs even while the log
    # is not written yet, so a job cancelled while still queued is caught too. The dscheck row
    # lives from the qsub insert (C queued, R running/complete) until cron removes it ~2 min
    # after completion, so a missing row means the job is gone. Until the ":qsub" step has
    # finished there is no cindex_submit and nothing to check.
    try:
        if cindex_submit is None:
            check_qsub = AsyncResult(f"{request_id}:qsub", app=celery_app)
            if check_qsub.successful():
                cindex_submit = check_qsub.result["cindex_submit"]
                # first time we see it: save it now, keeping the current state and meta, so
                # later polls read it from here once the ":qsub" result key has expired
                self.update_state(
                    state=prev_result.state,
                    meta=_meta(prev_meta if isinstance(prev_meta, dict) else {}),
                )

        # qsub exists: check the dscheck row for the submitted job
        if cindex_submit is not None:
            dscheck_status = _check_dscheck_row(cindex_submit)

            # If dscheck_status is None, it means the row for this cindex_submit is not found
            if dscheck_status is None:
                msg = f"Did not find dscheck row for cindex {cindex_submit} for PBS job"
                self.update_state(state=STATE_PBS_FAILURE_REPORTED, meta=_meta({"msg": msg}))
                # tell the portal too, otherwise a job cancelled in the background is never reported
                if callback_url:
                    call_back_json = job_response(
                        task_id=request_id, status=STATE_PBS_FAILURE_REPORTED, result={"msg": msg},
                        job_type="transform", completed=datetime.now(timezone.utc).isoformat(),
                    )
                    notify_callback.apply_async(
                        args=[call_back_json, callback_url],
                        task_id=f"{request_id}:callback-final",
                    )
                raise Ignore()
    except Ignore:
        # re-raise the Ignore exception to avoid get caught by the generic Exception handler
        raise
    except Exception as e:
        # Handle the case where checking the PBS job fails
        self.update_state(
            state="FAILURE",
            meta=_meta({"msg": f"Failed to check PBS job: {str(e)}"})
        )
        raise Ignore()

    # if log does not exist
    if not log_path.exists():
        # change state to PENDING
        self.update_state(state="PENDING", meta=_meta({"msg": "log file not written yet"}))
        # reschedule the watch to check again later (the write above refreshed the TTL)
        _reschedule_watch(self, request_id, callback_url, last_line, SEARCH_JSONL_INT, time.time())

    # Read the new log entries from the last processed line onward
    entries, raw_log = _read_complete_jsons(log_path)
    new_entries = entries[last_line:]
    new_last_line = len(entries)

    # mirror the log to Boreas (same bytes as on glade) whenever it grew. The terminal line
    # is a new line too, so the final log is uploaded before the callback goes out.
    # Best effort: a Boreas problem must not fail a healthy job.
    if new_entries:
        try:
            upload_job_log(raw_log, request_id)
        except Exception:
            logger.exception(f"Failed to upload log to Boreas for request_id={request_id}")

    # if no new entries, keep the state; only re-save it every HEARTBEAT_INT so the
    # result key's TTL does not expire while the job is silent
    if not new_entries:
        now = time.time()
        if last_refresh is None:
            last_refresh = now  # first silent poll: start the heartbeat clock
        elif now - last_refresh >= HEARTBEAT_INT:
            prev = self.AsyncResult(request_id)
            # PENDING can be real (log exists but has no complete line yet, state written by
            # the "log file not written yet" branch, with meta) or an expired/missing key
            # (no meta). Only the first has anything to restore.
            if prev.info:
                self.update_state(
                    state=prev.state,
                    meta={**(prev.info or {}),
                          "last_polled_at": datetime.now(timezone.utc).isoformat()},
                )
            last_refresh = now
        _reschedule_watch(self, request_id, callback_url, last_line, READ_JSONL_INT, last_refresh)

    # identify terminal lines (PBS_COMPLETED or PBS_FAILED) for PBS job completion state
    for entry in new_entries:
        msg = entry.get("process_message")
        if msg in (PBS_COMPLETED, PBS_FAILED):
            # Terminal line: the PBS job has finished, successfully or not
            if msg == PBS_COMPLETED:
                job_status = "SUCCESS"
                self.update_state(
                    state=job_status,
                    meta=_meta({"msg": f"Completed successfully. Check the {log_path} for details."})
                )
            else:
                job_status = STATE_PBS_FAILURE_REPORTED
                self.update_state(
                    state=job_status,  # FAILURE state is reserved for celery worker errors
                    meta=_meta({"msg": f"Completed with exit code 1 during transform. Check the {log_path} for details."})
                )

            # the "job_status" is the same state name written above (SUCCESS or PBS_FAILURE_REPORTED)
            # which is what we report back to the callback and ultimately to the client
            # result is the JSONL line itself (command, time_of_process, level, ...)
            call_back_json = job_response(
                task_id=request_id, status=job_status, result=entry,
                job_type="transform", completed=datetime.now(timezone.utc).isoformat(),
            )

            if callback_url:  # only if one was provided in the payload
                notify_callback.apply_async(
                    args=[call_back_json, callback_url],
                    task_id=f"{request_id}:callback-final",
                )
            # after notifying the callback, we can stop watching as the job has reached a terminal state
            # Ignore (no reschedule) keeps the state/meta written above; a return would overwrite them with SUCCESS
            raise Ignore()

    # only update the state with the latest entry if there are new entries
    latest = new_entries[-1]
    self.update_state(state=jsonl_to_celery_state(latest), meta=_meta(latest))

    _reschedule_watch(self, request_id, callback_url, new_last_line, READ_JSONL_INT, time.time())


def _reschedule_watch(task, request_id: str, callback_url: Optional[str], last_line: int, countdown: int,
                      last_refresh: Optional[float] = None):
    """Queue the next watch_job poll (a manual retry) under the same task id, then stop this
    run without writing a result ("Ignore()" keeps the current state/meta)."""
    watch_job.apply_async(
        args=[request_id, callback_url, last_line, last_refresh],
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
    callback_url = request_dict.get("CallbackUrl")

    # record that a callback is expected before anything can finish and fire it
    if callback_url:
        mark_callback_waiting(request_id)

    # Watcher first: it idles in PENDING until the job log appears, so starting it early is
    # harmless. If the chain publish then fails, the stray watcher is stopped below; the
    # other order could leave a running job that nothing tracks.
    try:
        watch_job.apply_async(args=[request_id, callback_url], task_id=request_id)
    except Exception as exc:
        try:
            celery_app.backend.mark_as_failure(request_id, exc)
            if callback_url:
                    call_back_json = job_response(
                        task_id=request_id, status="FAILURE", result={"msg": "watcher failed"},
                        job_type="transform", completed=datetime.now(timezone.utc).isoformat(),
                    )
                    notify_callback.apply_async(
                        args=[call_back_json,callback_url],
                        task_id=f"{request_id}:callback-final",
                    )
        except Exception:
            logger.exception(f"Failed to start watcher for request_id={request_id} with exception: {exc}")
        raise
        

    try:
        chain(
            create_boreas_artifacts.s(request_dict, request_id).set(task_id=f"{request_id}:artifacts"),
            # the first arg is the return of the previous task
            add_dscheck_download.s(specialist).set(task_id=f"{request_id}:download"),
            add_dscheck_qsub.s().set(task_id=f"{request_id}:qsub"),
        ).apply_async(link_error=chain_failed.s(request_id, callback_url))
    except Exception as exc:
        # nothing will run: stop the watcher (it checks for FAILURE on every poll).
        try:
            celery_app.backend.mark_as_failure(request_id, exc)
        except Exception:
            logger.exception(f"Could not mark request_id={request_id} failed after chain publish error")
        raise
