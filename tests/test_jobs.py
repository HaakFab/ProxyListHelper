"""portal/jobs.py's two load-bearing guarantees: a Scryfall outage must
propagate to a FAILED CheckRun (never look like a clean check), and a job
stuck RUNNING across a restart must be requeued rather than silently stall
its submission forever.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal import jobs
from portal.db import Base, init_engine, session_scope
from portal.models import Job, Round, Submission, SubmissionVersion, User
from proxy_parser.checker import ScryfallUnavailable


@pytest.fixture
def db():
    engine = init_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def submission_version(db):
    with session_scope() as s:
        u = User(display_name="Chris", person_slug="Chris", access_code_hash="x")
        s.add(u)
        r = Round(code="2611", name="Test", state="OPEN")
        s.add(r)
        s.flush()
        sub = Submission(round_id=r.id, user_id=u.id, title="Deck", ordinal=1, state="DRAFT")
        s.add(sub)
        s.flush()
        v = SubmissionVersion(
            submission_id=sub.id, version_no=1,
            raw_text="1 Sol Ring (LEA) 1\n1 Lightning Bolt (LEA) 161\n",
            content_sha256="x" * 64,
        )
        s.add(v)
        s.flush()
        return v.id


class TestRequeueStuckJobs:
    def test_running_job_is_requeued(self, db):
        with session_scope() as s:
            s.add(Job(kind="CHECK", state="RUNNING", submission_version_id=None))
        with session_scope() as s:
            count = jobs.requeue_stuck_jobs(s)
            assert count == 1
        with session_scope() as s:
            job = s.query(Job).one()
            assert job.state == "QUEUED"
            assert "Requeued" in job.error

    def test_queued_and_done_jobs_are_left_alone(self, db):
        with session_scope() as s:
            s.add(Job(kind="CHECK", state="QUEUED"))
            s.add(Job(kind="CHECK", state="DONE"))
        with session_scope() as s:
            assert jobs.requeue_stuck_jobs(s) == 0


class TestRunOneCheckJob:
    def test_scryfall_outage_produces_a_failed_check_never_a_clean_one(self, submission_version):
        """The single most important assertion in this module: an outage
        must FAIL the CheckRun, never leave it looking GREEN/clean."""
        with session_scope() as s:
            jobs.enqueue_check(s, submission_version)

        with patch(
            "proxy_parser.checker.scryfall_validator.ScryfallBatchValidator.validate_cards",
            side_effect=ScryfallUnavailable("simulated outage"),
        ):
            found = jobs.run_one_pending_job()
        assert found is True

        with session_scope() as s:
            version = s.get(SubmissionVersion, submission_version)
            check_run = version.check_run
            assert check_run.state == "FAILED"
            assert check_run.scryfall_ok is False
            assert check_run.traffic_light is None  # never set to GREEN/YELLOW/RED
            assert version.submission.state == "CHECK_FAILED"

    def test_successful_check_marks_the_run_done(self, submission_version):
        with session_scope() as s:
            jobs.enqueue_check(s, submission_version)

        with patch(
            "proxy_parser.checker.scryfall_validator.ScryfallBatchValidator.validate_cards",
            return_value=[],
        ):
            jobs.run_one_pending_job()

        with session_scope() as s:
            version = s.get(SubmissionVersion, submission_version)
            check_run = version.check_run
            assert check_run.state == "DONE"
            assert check_run.scryfall_ok is True
            assert check_run.total_cards == 2
            assert check_run.traffic_light in ("GREEN", "YELLOW")
            assert version.submission.state in ("CLEAN", "NEEDS_REVIEW")

    def test_empty_queue_returns_false(self, db):
        assert jobs.run_one_pending_job() is False

    def test_failed_job_is_retried_up_to_max_attempts(self, submission_version):
        with session_scope() as s:
            job = jobs.enqueue_check(s, submission_version)
            job_id = job.id

        with patch(
            "proxy_parser.checker.scryfall_validator.ScryfallBatchValidator.validate_cards",
            side_effect=ScryfallUnavailable("simulated outage"),
        ):
            jobs.run_one_pending_job()

        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job.attempts == 1
            assert job.state == "QUEUED"  # under MAX_ATTEMPTS, eligible to retry

    def test_missing_submission_version_fails_the_job_cleanly(self, db):
        """submission_version_id is a real FK with ondelete=CASCADE, so a Job
        can never outlive its version in practice -- but the null case
        (submission_version_id left unset) exercises the same defensive
        guard in _run_check_job without fighting FK enforcement to simulate
        an otherwise-impossible dangling reference."""
        with session_scope() as s:
            job = Job(kind="CHECK", state="QUEUED", submission_version_id=None)
            s.add(job)
            s.flush()
            job_id = job.id

        jobs.run_one_pending_job()

        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job.state == "FAILED"
