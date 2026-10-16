"""GDPR self-deletion must not leave orphaned rows or raise an IntegrityError.

This exercises the FULL cascade from User down through every table that
references it, including the two-hop case (User -> Order -> PriceSnapshot /
OrderSubmission / PaymentRecord) where PriceSnapshot has no ORM-level cascade
relationship from Order and relies entirely on the database's own
ON DELETE CASCADE (see models.py) -- worth verifying directly rather than
assuming it works.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.db import Base, init_engine, session_scope
from portal.models import (
    AuditLog,
    CheckRun,
    LoginToken,
    Order,
    OrderSubmission,
    PaymentRecord,
    PriceSnapshot,
    Round,
    Session as SessionModel,
    Submission,
    SubmissionVersion,
    User,
    utcnow,
)


@pytest.fixture
def db():
    engine = init_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield


def _fully_wired_user(session) -> int:
    """One user with a submission (+version +check_run), a session, a login
    token, and a paid order (+price_snapshot +order_submission +payment)."""
    u = User(display_name="Chris", person_slug="Chris", access_code_hash="x")
    r = Round(code="2611", name="Test", state="OPEN")
    session.add_all([u, r])
    session.flush()

    sub = Submission(round_id=r.id, user_id=u.id, title="Deck", ordinal=1, state="FROZEN")
    session.add(sub)
    session.flush()

    version = SubmissionVersion(
        submission_id=sub.id, version_no=1, raw_text="1 Sol Ring", content_sha256="a" * 64,
    )
    session.add(version)
    session.flush()

    check_run = CheckRun(
        submission_version_id=version.id, state="DONE", scryfall_ok=True,
        traffic_light="GREEN", total_cards=1, unique_lines=1,
    )
    session.add(check_run)

    order = Order(round_id=r.id, user_id=u.id, seq=1, reference_code="MTG-2611-CHRIS-AAAA", state="PAID")
    session.add(order)
    session.flush()

    snapshot = PriceSnapshot(
        order_id=order.id, cost_per_card_cents=13, cost_per_list_cents=26,
        processing_charge_cents=0, n_cards=1, n_lists=1, total_cents=39, breakdown_json="{}",
    )
    session.add(snapshot)
    session.flush()
    order.price_snapshot_id = snapshot.id

    session.add(OrderSubmission(
        order_id=order.id, submission_id=sub.id, submission_version_id=version.id, n_cards=1,
    ))
    session.add(PaymentRecord(
        order_id=order.id, amount_cents=39, kind="PAYPAL", received_on=date.today(),
        recorded_by_user_id=u.id,
    ))
    session.add(LoginToken(user_id=u.id, token_hash="tok" + "x" * 61, expires_at=utcnow()))
    session.add(SessionModel(user_id=u.id, token_hash="sess" + "y" * 60, expires_at=utcnow()))
    session.flush()

    return u.id, order.id, sub.id, version.id, snapshot.id


class TestFullCascadeDelete:
    def test_deleting_a_user_removes_every_dependent_row(self, db):
        with session_scope() as s:
            user_id, order_id, sub_id, version_id, snapshot_id = _fully_wired_user(s)

        with session_scope() as s:
            user = s.get(User, user_id)
            s.delete(user)

        with session_scope() as s:
            assert s.get(User, user_id) is None
            assert s.get(Order, order_id) is None
            assert s.get(Submission, sub_id) is None
            assert s.get(SubmissionVersion, version_id) is None
            assert s.get(PriceSnapshot, snapshot_id) is None, (
                "PriceSnapshot survived -- the DB-level ON DELETE CASCADE on "
                "order_id did not fire"
            )
            assert s.query(OrderSubmission).count() == 0
            assert s.query(PaymentRecord).count() == 0
            assert s.query(LoginToken).count() == 0
            assert s.query(SessionModel).count() == 0
            assert s.query(CheckRun).count() == 0

    def test_no_integrity_error_is_raised(self, db):
        """The concrete failure mode this test exists to catch: an
        IntegrityError from a dependent row the cascade chain missed."""
        with session_scope() as s:
            user_id, *_ = _fully_wired_user(s)

        with session_scope() as s:
            user = s.get(User, user_id)
            s.delete(user)  # must not raise


class TestAuditLogSurvivesUserDeletion:
    def test_audit_log_keeps_the_row_with_actor_set_to_null(self, db):
        """AuditLog.actor_user_id has no ondelete=CASCADE -- it should
        survive deletion (accountability), with the FK set NULL rather than
        blocking the delete or orphaning silently with a dangling reference."""
        with session_scope() as s:
            user_id, *_ = _fully_wired_user(s)
            s.add(AuditLog(actor_user_id=user_id, actor_kind="USER",
                            action="test.action", subject_type="Test", subject_id="1"))

        with session_scope() as s:
            user = s.get(User, user_id)
            s.delete(user)

        with session_scope() as s:
            row = s.query(AuditLog).filter(AuditLog.action == "test.action").one()
            assert row.actor_user_id is None
