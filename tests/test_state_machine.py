"""Guarded transitions for Submission and Order. Every illegal transition must
raise; every legal one must produce exactly the state and side effects
described in portal_implementation.md §7 -- especially the two rules that
protect money: FROZEN submissions refuse edits outright, and editing a
LOCKED (unpaid) order's submission voids its price snapshot.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.db import Base, init_engine, session_scope
from portal import models
from portal.state_machine import (
    IllegalTransition,
    accept,
    admin_unlock,
    admin_unlock_order,
    apply_check_result,
    cancel_order,
    edit,
    freeze,
    freeze_order,
    lock_for_order,
    lock_order,
    mark_check_failed,
    mark_refunded,
    order_edit_reopens,
    record_payment_total,
    start_checking,
    withdraw,
)


@pytest.fixture
def db():
    """A fresh in-memory database per test. No teardown drop needed: the next
    test calls init_engine() again, which builds an entirely new (empty)
    :memory: database -- there is nothing shared to clean up, and dropping
    tables here would fight SQLite's FK-enforced DROP ordering for no benefit.
    """
    engine = init_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def user_and_round(db):
    with session_scope() as s:
        u = models.User(display_name="Chris", person_slug="Chris", access_code_hash="x")
        s.add(u)
        r = models.Round(code="2611", name="Test Round", state="OPEN")
        s.add(r)
        s.flush()
        return u.id, r.id


def _make_submission(session, round_id, user_id, ordinal=1, state="DRAFT"):
    sub = models.Submission(round_id=round_id, user_id=user_id, title="Deck", ordinal=ordinal, state=state)
    session.add(sub)
    session.flush()
    return sub


def _make_version(session, submission_id, version_no=1):
    v = models.SubmissionVersion(
        submission_id=submission_id, version_no=version_no, raw_text="1 Sol Ring",
        content_sha256="x" * 64,
    )
    session.add(v)
    session.flush()
    return v


# --- Submission ---------------------------------------------------------------

class TestSubmissionChecking:
    def test_start_checking_sets_state(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id)
            start_checking(sub)
            assert sub.state == "CHECKING"

    @pytest.mark.parametrize(
        "traffic_light,expected_state",
        [("GREEN", "CLEAN"), ("YELLOW", "NEEDS_REVIEW"), ("RED", "NEEDS_FIXES")],
    )
    def test_apply_check_result_maps_traffic_light(self, user_and_round, traffic_light, expected_state):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="CHECKING")
            apply_check_result(sub, traffic_light)
            assert sub.state == expected_state

    def test_apply_check_result_requires_checking_state(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="DRAFT")
            with pytest.raises(IllegalTransition):
                apply_check_result(sub, "GREEN")

    def test_unknown_traffic_light_rejected(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="CHECKING")
            with pytest.raises(IllegalTransition):
                apply_check_result(sub, "PURPLE")

    def test_mark_check_failed(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="CHECKING")
            mark_check_failed(sub)
            assert sub.state == "CHECK_FAILED"


class TestAccept:
    @pytest.mark.parametrize("state", ["CLEAN", "NEEDS_REVIEW"])
    def test_accept_from_clean_or_needs_review(self, user_and_round, state):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state=state)
            v = _make_version(s, sub.id)
            accept(sub, v.id)
            assert sub.state == "ACCEPTED"
            assert sub.accepted_version_id == v.id

    @pytest.mark.parametrize("state", ["DRAFT", "CHECKING", "NEEDS_FIXES", "FROZEN"])
    def test_accept_rejected_from_other_states(self, user_and_round, state):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state=state)
            with pytest.raises(IllegalTransition):
                accept(sub, 1)


class TestEdit:
    def test_edit_from_needs_fixes_returns_to_checking(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="NEEDS_FIXES")
            edit(s, sub)
            assert sub.state == "CHECKING"

    def test_edit_clears_accepted_version(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="ACCEPTED")
            sub.accepted_version_id = 999
            edit(s, sub)
            assert sub.accepted_version_id is None

    def test_edit_refuses_frozen_submission(self, user_and_round):
        """The money-safety guarantee: a paid, frozen list cannot be edited
        out from under an order without an admin unlock."""
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="FROZEN")
            with pytest.raises(IllegalTransition):
                edit(s, sub)

    @pytest.mark.parametrize("state", ["IN_PRINT_RUN", "DONE"])
    def test_edit_refuses_terminal_states(self, user_and_round, state):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state=state)
            with pytest.raises(IllegalTransition):
                edit(s, sub)

    def test_edit_reopens_a_locked_unpaid_order(self, user_and_round):
        """The other half of the money-safety guarantee: editing a LOCKED
        (unpaid) submission must drop its order back to OPEN and void the
        price snapshot -- otherwise the shown price silently goes stale."""
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="LOCKED")
            version = _make_version(s, sub.id)
            order = _make_order(s, round_id, user_id, state="LOCKED", with_price_snapshot=True)
            s.add(models.OrderSubmission(
                order_id=order.id, submission_id=sub.id,
                submission_version_id=version.id, n_cards=10,
            ))
            s.flush()

            edit(s, sub)

            assert sub.state == "CHECKING"
            assert order.state == "OPEN"
            assert order.price_snapshot_id is None

    def test_edit_does_not_touch_an_already_paid_order(self, user_and_round):
        """If the submission itself isn't FROZEN yet but somehow its order
        already is (shouldn't happen, but must not silently corrupt state if
        it does): only reopen an order that is exactly LOCKED."""
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="LOCKED")
            version = _make_version(s, sub.id)
            order = _make_order(s, round_id, user_id, state="PAID", with_price_snapshot=True)
            snapshot_id = order.price_snapshot_id
            s.add(models.OrderSubmission(
                order_id=order.id, submission_id=sub.id,
                submission_version_id=version.id, n_cards=10,
            ))
            s.flush()

            edit(s, sub)

            assert order.state == "PAID"  # untouched
            assert order.price_snapshot_id == snapshot_id


class TestWithdrawLockFreeze:
    def test_withdraw_sets_state_and_timestamp(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="ACCEPTED")
            withdraw(sub)
            assert sub.state == "WITHDRAWN"
            assert sub.withdrawn_at is not None

    def test_cannot_withdraw_frozen(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="FROZEN")
            with pytest.raises(IllegalTransition):
                withdraw(sub)

    def test_lock_for_order_requires_accepted(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="ACCEPTED")
            lock_for_order(sub)
            assert sub.state == "LOCKED"

            sub2 = _make_submission(s, round_id, user_id, ordinal=2, state="CLEAN")
            with pytest.raises(IllegalTransition):
                lock_for_order(sub2)

    def test_freeze_requires_locked(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="LOCKED")
            freeze(sub)
            assert sub.state == "FROZEN"

    def test_admin_unlock_from_frozen_returns_to_accepted(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            sub = _make_submission(s, round_id, user_id, state="FROZEN")
            admin_unlock(sub)
            assert sub.state == "ACCEPTED"


# --- Order ----------------------------------------------------------------------

def _make_price_snapshot(session, order_id, total_cents=1391):
    snap = models.PriceSnapshot(
        order_id=order_id, cost_per_card_cents=13, cost_per_list_cents=26,
        processing_charge_cents=0, n_cards=100, n_lists=1,
        total_cents=total_cents, breakdown_json="{}",
    )
    session.add(snap)
    session.flush()
    return snap


def _make_order(session, round_id, user_id, state="OPEN", with_price_snapshot=False, seq=1):
    """`with_price_snapshot=True` inserts a REAL PriceSnapshot row and wires
    it up -- price_snapshot_id is a foreign key, so a test cannot just poke in
    an arbitrary integer once FK enforcement is actually active (see the
    StaticPool fix in portal/db.py: before it, FK checks silently no-op'd
    against a fresh empty in-memory DB on the second connection)."""
    order = models.Order(
        round_id=round_id, user_id=user_id, seq=seq,
        reference_code=f"MTG-2611-CHRIS-{seq:04d}", state=state,
    )
    session.add(order)
    session.flush()
    if with_price_snapshot:
        snap = _make_price_snapshot(session, order.id)
        order.price_snapshot_id = snap.id
        session.flush()
    return order


class TestOrderLockAndReopen:
    def test_lock_order_from_open(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="OPEN")
            snap = _make_price_snapshot(s, order.id)
            lock_order(order, price_snapshot_id=snap.id, reference_code="MTG-2611-CHRIS-ZZZZ")
            assert order.state == "LOCKED"
            assert order.price_snapshot_id == snap.id
            assert order.locked_at is not None

    def test_lock_order_rejected_unless_open(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="LOCKED")
            # Raised before any write is attempted, so this never touches the
            # (nonexistent) price_snapshot_id -- no FK violation to worry about.
            with pytest.raises(IllegalTransition):
                lock_order(order, price_snapshot_id=999999, reference_code="X")

    def test_order_edit_reopens_only_acts_on_locked(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="PAID", with_price_snapshot=True)
            snapshot_id = order.price_snapshot_id
            order_edit_reopens(order)
            assert order.state == "PAID"  # untouched -- not LOCKED, so no-op
            assert order.price_snapshot_id == snapshot_id


class TestPaymentRecording:
    def test_partial_payment(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="LOCKED", with_price_snapshot=True)
            record_payment_total(order, total_paid_cents=500, price_total_cents=1391)
            assert order.state == "PARTIALLY_PAID"

    def test_full_payment_marks_paid(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="LOCKED", with_price_snapshot=True)
            record_payment_total(order, total_paid_cents=1391, price_total_cents=1391)
            assert order.state == "PAID"
            assert order.paid_confirmed_at is not None

    def test_overpayment_still_marks_paid(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="LOCKED", with_price_snapshot=True)
            record_payment_total(order, total_paid_cents=2000, price_total_cents=1391)
            assert order.state == "PAID"

    def test_zero_payment_reverts_to_locked(self, user_and_round):
        """A refund that brings the running total back to zero must not leave
        the order stuck in PARTIALLY_PAID."""
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="PARTIALLY_PAID", with_price_snapshot=True)
            record_payment_total(order, total_paid_cents=0, price_total_cents=1391)
            assert order.state == "LOCKED"

    def test_cannot_record_payment_against_open_order(self, user_and_round):
        """An OPEN order has no price snapshot yet -- there is nothing to pay against."""
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="OPEN")
            with pytest.raises(IllegalTransition):
                record_payment_total(order, total_paid_cents=100, price_total_cents=1391)


class TestFreezeCancelRefund:
    def test_freeze_order_requires_paid(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="PAID", with_price_snapshot=True)
            freeze_order(order)
            assert order.state == "FROZEN"
            assert order.frozen_at is not None

    def test_freeze_order_rejected_unless_paid(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="PARTIALLY_PAID")
            with pytest.raises(IllegalTransition):
                freeze_order(order)

    def test_admin_unlock_order_clears_everything(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="FROZEN", with_price_snapshot=True)
            order.paid_confirmed_at = order.frozen_at = models.utcnow()
            admin_unlock_order(order)
            assert order.state == "OPEN"
            assert order.price_snapshot_id is None
            assert order.paid_confirmed_at is None
            assert order.frozen_at is None

    def test_cancel_requires_a_reason(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="OPEN")
            with pytest.raises(ValueError):
                cancel_order(order, reason="   ")

    def test_cancel_rejects_terminal_states(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="COMPLETED")
            with pytest.raises(IllegalTransition):
                cancel_order(order, reason="duplicate order")

    def test_mark_refunded_requires_paid_or_frozen(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="PAID")
            mark_refunded(order)
            assert order.state == "REFUNDED"

    def test_mark_refunded_rejects_open(self, user_and_round):
        user_id, round_id = user_and_round
        with session_scope() as s:
            order = _make_order(s, round_id, user_id, state="OPEN")
            with pytest.raises(IllegalTransition):
                mark_refunded(order)
