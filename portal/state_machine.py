"""Guarded state transitions for Submission and Order (portal_implementation.md §7).

Every transition that matters for money or for what gets printed goes through
one of these functions rather than a route handler setting `.state = "..."`
directly. That's what makes "can this legally happen" a single place to audit,
and what stops a route from accidentally skipping a side effect (e.g. voiding
a price snapshot when a paid-adjacent order is edited).
"""
from __future__ import annotations

from sqlalchemy.orm import Session, object_session

from .models import (
    Order,
    OrderState,
    OrderSubmission,
    Submission,
    SubmissionState,
    utcnow,
)


class IllegalTransition(ValueError):
    pass


def _expire_relationship(obj, *attr_names: str) -> None:
    """Call this immediately after directly reassigning a *_id foreign key
    column (submission.accepted_version_id = ..., order.price_snapshot_id =
    ..., etc).

    SQLAlchemy does NOT automatically refresh an already-cached many-to-one
    relationship attribute when you reassign its raw FK column directly --
    only assigning through the relationship itself keeps both sides in sync.
    If something in this session already read e.g. `submission.accepted_version`
    once, reassigning `accepted_version_id` and reading `.accepted_version`
    again would silently return the OLD, now-superseded object. That is
    exactly the kind of bug that could bill a locked order for a version that
    was already replaced. Expiring forces the next access to re-query.

    A no-op for a transient/pending object (nothing persisted yet to be stale).
    """
    session = object_session(obj)
    if session is not None and obj in session:
        try:
            session.expire(obj, list(attr_names))
        except Exception:
            pass  # transient/pending object -- nothing cached to invalidate


# --- Submission --------------------------------------------------------------

def attach_new_version(submission: Submission, version_id: int) -> None:
    """Sets Submission.current_version_id -- the ONLY place that should, so
    the _expire_relationship call is never forgotten by a route. Does not
    itself change `state`; call `edit()` alongside this for an existing
    submission, or leave state as DRAFT for a brand-new one."""
    submission.current_version_id = version_id
    _expire_relationship(submission, "current_version")


def start_checking(submission: Submission) -> None:
    submission.state = SubmissionState.CHECKING.value
    submission.updated_at = utcnow()


def apply_check_result(submission: Submission, traffic_light: str) -> None:
    """CHECKING -> CLEAN | NEEDS_REVIEW | NEEDS_FIXES, based on the verdict."""
    if submission.state != SubmissionState.CHECKING.value:
        raise IllegalTransition(
            f"apply_check_result requires CHECKING, submission {submission.id} is "
            f"{submission.state}"
        )
    mapping = {
        "GREEN": SubmissionState.CLEAN,
        "YELLOW": SubmissionState.NEEDS_REVIEW,
        "RED": SubmissionState.NEEDS_FIXES,
    }
    if traffic_light not in mapping:
        raise IllegalTransition(f"unknown traffic light {traffic_light!r}")
    submission.state = mapping[traffic_light].value
    submission.updated_at = utcnow()


def mark_check_failed(submission: Submission) -> None:
    submission.state = SubmissionState.CHECK_FAILED.value
    submission.updated_at = utcnow()


def accept(submission: Submission, version_id: int) -> None:
    """User accepts the auto-fixed (or already-clean) list."""
    if submission.state not in (SubmissionState.CLEAN.value, SubmissionState.NEEDS_REVIEW.value):
        raise IllegalTransition(
            f"cannot accept submission {submission.id} in state {submission.state}"
        )
    submission.accepted_version_id = version_id
    _expire_relationship(submission, "accepted_version")
    submission.state = SubmissionState.ACCEPTED.value
    submission.updated_at = utcnow()


def edit(session: Session, submission: Submission) -> None:
    """A new version was just attached as current_version_id; this reflects
    the resulting state change and, critically, drops a not-yet-paid order
    back to OPEN and voids its price snapshot -- see order_edit_reopens below.
    Refuses outright if the submission is FROZEN (paid): only an admin unlock
    can move it, via admin_unlock().
    """
    if submission.state == SubmissionState.FROZEN.value:
        raise IllegalTransition(
            f"submission {submission.id} is frozen (paid); admin must unlock its order first"
        )
    if submission.state in (SubmissionState.IN_PRINT_RUN.value, SubmissionState.DONE.value):
        raise IllegalTransition(f"submission {submission.id} is already {submission.state}")

    if submission.state == SubmissionState.LOCKED.value:
        order = _find_order_for_submission(session, submission)
        if order is not None and order.state == OrderState.LOCKED.value:
            order_edit_reopens(order)
            _revert_sibling_submissions_to_accepted(session, order, skip_submission_id=submission.id)

    submission.accepted_version_id = None
    _expire_relationship(submission, "accepted_version")
    submission.state = SubmissionState.CHECKING.value
    submission.updated_at = utcnow()


def _find_order_for_submission(session: Session, submission: Submission) -> Order | None:
    link = (
        session.query(OrderSubmission)
        .filter(OrderSubmission.submission_id == submission.id)
        .order_by(OrderSubmission.order_id.desc())
        .first()
    )
    return session.get(Order, link.order_id) if link else None


def _revert_sibling_submissions_to_accepted(session: Session, order: Order, skip_submission_id: int) -> None:
    """When one submission's edit reopens its order, every OTHER submission
    that was LOCKED as part of that same order must revert to ACCEPTED --
    otherwise they stay stranded at LOCKED while their order is OPEN, and
    silently drop out of the next confirm_order() price snapshot (they would
    no longer be an ACCEPTED submission the next time pricing runs, even
    though nothing about their own content changed).
    """
    links = session.query(OrderSubmission).filter(OrderSubmission.order_id == order.id).all()
    for link in links:
        if link.submission_id == skip_submission_id:
            continue
        sibling = session.get(Submission, link.submission_id)
        if sibling is not None and sibling.state == SubmissionState.LOCKED.value:
            sibling.state = SubmissionState.ACCEPTED.value
            sibling.updated_at = utcnow()


def withdraw(submission: Submission) -> None:
    if submission.state == SubmissionState.FROZEN.value:
        raise IllegalTransition(f"cannot withdraw a frozen (paid) submission {submission.id}")
    submission.state = SubmissionState.WITHDRAWN.value
    submission.withdrawn_at = utcnow()


def lock_for_order(submission: Submission) -> None:
    if submission.state != SubmissionState.ACCEPTED.value:
        raise IllegalTransition(f"submission {submission.id} must be ACCEPTED to lock, is {submission.state}")
    submission.state = SubmissionState.LOCKED.value


def freeze(submission: Submission) -> None:
    if submission.state != SubmissionState.LOCKED.value:
        raise IllegalTransition(f"submission {submission.id} must be LOCKED to freeze, is {submission.state}")
    submission.state = SubmissionState.FROZEN.value


def admin_unlock(submission: Submission) -> None:
    """Only path back from FROZEN. Always audit-logged by the caller."""
    if submission.state not in (SubmissionState.FROZEN.value, SubmissionState.LOCKED.value):
        raise IllegalTransition(f"submission {submission.id} is not locked/frozen, is {submission.state}")
    submission.state = SubmissionState.ACCEPTED.value
    submission.updated_at = utcnow()


# --- Order --------------------------------------------------------------------

def lock_order(order: Order, price_snapshot_id: int, reference_code: str) -> None:
    if order.state != OrderState.OPEN.value:
        raise IllegalTransition(f"order {order.id} must be OPEN to lock, is {order.state}")
    order.state = OrderState.LOCKED.value
    order.price_snapshot_id = price_snapshot_id
    _expire_relationship(order, "price_snapshot")
    order.reference_code = reference_code
    order.locked_at = utcnow()


def order_edit_reopens(order: Order) -> None:
    """Called when one of the order's submissions is edited while the order
    is merely LOCKED (not yet paid): the price snapshot no longer reflects
    reality, so drop back to OPEN. An order that is PAID or later is
    unaffected by this function -- edits are refused before reaching here
    (see state_machine.edit's FROZEN guard)."""
    if order.state == OrderState.LOCKED.value:
        order.state = OrderState.OPEN.value
        order.price_snapshot_id = None
        _expire_relationship(order, "price_snapshot")
        order.locked_at = None


def record_payment_total(order: Order, total_paid_cents: int, price_total_cents: int) -> None:
    """Recomputes order.state from the sum of its PaymentRecords. Called after
    inserting a new PaymentRecord -- this function does not insert one itself,
    it only reflects the new total into the order's state."""
    if order.state not in (
        OrderState.LOCKED.value,
        OrderState.PARTIALLY_PAID.value,
        OrderState.PAID.value,
    ):
        raise IllegalTransition(f"cannot record a payment against order {order.id} in state {order.state}")
    if total_paid_cents >= price_total_cents:
        order.state = OrderState.PAID.value
        order.paid_confirmed_at = order.paid_confirmed_at or utcnow()
    elif total_paid_cents > 0:
        order.state = OrderState.PARTIALLY_PAID.value
    else:
        order.state = OrderState.LOCKED.value


def freeze_order(order: Order) -> None:
    if order.state != OrderState.PAID.value:
        raise IllegalTransition(f"order {order.id} must be PAID to freeze, is {order.state}")
    order.state = OrderState.FROZEN.value
    order.frozen_at = utcnow()


def admin_unlock_order(order: Order) -> None:
    if order.state not in (
        OrderState.LOCKED.value,
        OrderState.PARTIALLY_PAID.value,
        OrderState.PAID.value,
        OrderState.FROZEN.value,
    ):
        raise IllegalTransition(f"order {order.id} cannot be unlocked from state {order.state}")
    order.state = OrderState.OPEN.value
    order.price_snapshot_id = None
    _expire_relationship(order, "price_snapshot")
    order.locked_at = None
    order.paid_confirmed_at = None
    order.frozen_at = None


def cancel_order(order: Order, reason: str) -> None:
    """`reason` is required but not stored here -- it belongs in an AuditLog
    row, which is the caller's job (see routes/admin.py). Keeping it a
    mandatory parameter of this function is deliberate: it stops a cancel
    route from ever being wired up without capturing why."""
    if order.state in (OrderState.COMPLETED.value, OrderState.CANCELLED.value):
        raise IllegalTransition(f"order {order.id} is already terminal ({order.state})")
    if not reason.strip():
        raise ValueError("a cancellation reason is required")
    order.state = OrderState.CANCELLED.value


def mark_refunded(order: Order) -> None:
    if order.state not in (OrderState.PAID.value, OrderState.FROZEN.value):
        raise IllegalTransition(f"order {order.id} must be PAID/FROZEN to refund, is {order.state}")
    order.state = OrderState.REFUNDED.value
