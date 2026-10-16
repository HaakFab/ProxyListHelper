"""The in-process background job runner (portal_implementation.md §12).

One thread, draining the `jobs` table. No Celery/Redis: the Scryfall checker
is blocking `requests` code that has to be serialized to respect rate limits
anyway, so a second worker thread would buy nothing but complexity.

Two things this module is careful about, because getting either wrong has
already caused a real bug once in this project's history (see
proxy_parser/checker/scryfall_validator.py's fixed silent-failure bug):

  1. ScryfallUnavailable must propagate all the way to a FAILED CheckRun with
     scryfall_ok=False. It is never caught-and-ignored.
  2. A job stuck in RUNNING when the process restarts (crash, redeploy) is
     requeued at startup -- otherwise its submission spins on "wird geprüft"
     forever, and nobody notices because nothing errored.
"""
import hashlib
import logging
import threading
import traceback

from sqlalchemy.orm import Session

from . import checking, state_machine
from .db import session_scope
from .db_cache import SqliteScryfallCache
from .models import CheckRun, Job, SubmissionVersion, utcnow
from proxy_parser.checker import ScryfallUnavailable

logger = logging.getLogger("portal.jobs")

MAX_ATTEMPTS = 3
POLL_INTERVAL_SECONDS = 1.0
# Backoff before a failed check is retried: 1 min, 5 min, 15 min.
RETRY_DELAYS_SECONDS = (60, 300, 900)


def enqueue_check(session: Session, submission_version_id: int) -> Job:
    job = Job(kind="CHECK", submission_version_id=submission_version_id, state="QUEUED")
    session.add(job)
    session.flush()
    return job


def requeue_stuck_jobs(session: Session) -> int:
    """Call once at process startup, before the worker thread starts. A job
    left RUNNING means the process died mid-check; nothing about that job's
    progress can be trusted, so it goes back to QUEUED with attempts reset
    to 0 (a crash is not the job's fault)."""
    stuck = session.query(Job).filter(Job.state == "RUNNING").all()
    for job in stuck:
        job.state = "QUEUED"
        job.started_at = None
        job.error = f"Requeued after an unclean restart (was RUNNING at {utcnow().isoformat()})"
    return len(stuck)


def _claim_next_job(session: Session) -> Job | None:
    """Picks the oldest ready job. True atomicity across processes would need
    a `SELECT ... FOR UPDATE`-style claim, but this runs as a single
    in-process thread against a single-writer SQLite database, so a plain
    query-then-update is sufficient -- there is no second worker to race
    against."""
    now = utcnow()
    candidates = (
        session.query(Job)
        .filter(Job.state == "QUEUED")
        .order_by(Job.created_at.asc())
        .all()
    )
    for job in candidates:
        if job.attempts == 0:
            return job
        # Respect the backoff schedule for a retried job.
        delay = RETRY_DELAYS_SECONDS[min(job.attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        if job.finished_at is None or (now - job.finished_at).total_seconds() >= delay:
            return job
    return None


def _run_check_job(session: Session, job: Job) -> None:
    version = (
        session.get(SubmissionVersion, job.submission_version_id)
        if job.submission_version_id is not None
        else None
    )
    if version is None:
        job.state = "FAILED"
        job.error = "submission_version no longer exists"
        return

    submission = version.submission
    user = submission.user
    check_run = version.check_run
    if check_run is None:
        check_run = CheckRun(submission_version_id=version.id, state="RUNNING")
        session.add(check_run)
    check_run.state = "RUNNING"
    check_run.started_at = utcnow()
    session.flush()

    state_machine.start_checking(submission)
    session.flush()

    try:
        cache = SqliteScryfallCache(session)
        outcome = checking.run_check(
            raw_text=version.raw_text,
            display_name=user.display_name,
            person_slug=user.person_slug,
            ordinal=submission.ordinal,
            title=submission.title,
            cache=cache,
        )
    except ScryfallUnavailable as ex:
        # THE non-negotiable: never let this look like a clean check. See the
        # module docstring and tests/test_scryfall_resilience.py.
        check_run.state = "FAILED"
        check_run.scryfall_ok = False
        check_run.error = str(ex)
        check_run.finished_at = utcnow()
        state_machine.mark_check_failed(submission)
        job.error = str(ex)
        raise
    except Exception as ex:
        check_run.state = "FAILED"
        check_run.scryfall_ok = False
        check_run.error = f"{type(ex).__name__}: {ex}"
        check_run.finished_at = utcnow()
        state_machine.mark_check_failed(submission)
        job.error = str(ex)
        raise

    check_run.state = "DONE"
    check_run.scryfall_ok = True
    check_run.traffic_light = outcome.traffic_light
    check_run.total_cards = outcome.total_cards
    check_run.unique_lines = outcome.unique_lines
    check_run.audit_json = outcome.to_audit_json()
    check_run.fixed_text = outcome.fixed_text
    check_run.fixed_sha256 = hashlib.sha256(outcome.fixed_text.encode("utf-8")).hexdigest()
    check_run.finished_at = utcnow()

    state_machine.apply_check_result(submission, outcome.traffic_light)
    job.state = "DONE"
    job.finished_at = utcnow()


def run_one_pending_job() -> bool:
    """Runs at most one job. Returns True if a job was found (whether it
    succeeded or failed), False if the queue was empty. Split out from the
    poll loop so it's independently testable without threads."""
    with session_scope() as session:
        job = _claim_next_job(session)
        if job is None:
            return False
        job.state = "RUNNING"
        job.started_at = utcnow()
        job.attempts += 1
        session.flush()

        try:
            _run_check_job(session, job)
        except Exception:
            job.state = "FAILED" if job.attempts >= MAX_ATTEMPTS else "QUEUED"
            job.finished_at = utcnow()
            if not job.error:
                job.error = traceback.format_exc(limit=5)
            logger.warning("job %s failed (attempt %s/%s): %s", job.id, job.attempts, MAX_ATTEMPTS, job.error)
    return True


def run_worker_loop(stop_event: threading.Event) -> None:
    """The Phase-1 background thread. Started once at app startup (see
    portal/main.py), stopped on shutdown."""
    while not stop_event.is_set():
        try:
            found = run_one_pending_job()
        except Exception:
            logger.exception("unexpected error in job loop")
            found = False
        if not found:
            stop_event.wait(POLL_INTERVAL_SECONDS)
