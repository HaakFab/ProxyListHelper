"""Order creation/locking: turns a user's ACCEPTED submissions in a round into
a locked Order with an immutable PriceSnapshot and a reference code.

Kept separate from routes/orders.py so the pricing/locking logic is testable
without spinning up the ASGI app.
"""
import json

from sqlalchemy.orm import Session

from . import state_machine
from .models import Order, OrderSubmission, PriceSnapshot, Round, Submission, User
from .pricing import Rates, SubmissionCharge, compute_breakdown
from .security import generate_readable_code
from .slugs import make_reference_code


class NoAcceptedSubmissions(ValueError):
    pass


class SubmissionNotReady(ValueError):
    """An ACCEPTED submission with no completed CheckRun on its accepted
    version. Should be unreachable (accept() requires CLEAN/NEEDS_REVIEW,
    which only follow a DONE check run) -- raised rather than silently
    billing 0 cards if that invariant is ever broken."""


def _charge_for(submission: Submission) -> SubmissionCharge:
    """Bills exactly what was ACCEPTED -- not whatever the submission's
    current_version happens to be. In the ACCEPTED state these are the same
    row (accept() sets accepted_version_id = current_version_id, and any
    later edit immediately clears accepted_version_id and leaves ACCEPTED),
    but pricing reads accepted_version explicitly so that invariant is never
    load-bearing for correctness here.
    """
    version = submission.accepted_version
    if version is None or version.check_run is None or version.check_run.state != "DONE":
        raise SubmissionNotReady(
            f"submission {submission.id} has no completed check on its accepted version"
        )
    return SubmissionCharge(
        submission_id=submission.id,
        title=submission.title,
        n_cards=version.check_run.total_cards,
        content_sha256=version.check_run.fixed_sha256 or version.content_sha256,
    )


def round_rates(round_: Round) -> Rates:
    return Rates(
        cost_per_card_cents=round_.cost_per_card_cents,
        cost_per_list_cents=round_.cost_per_list_cents,
        processing_charge_cents=round_.processing_charge_cents,
    )


def accepted_submissions(session: Session, round_id: int, user_id: int) -> list[Submission]:
    return (
        session.query(Submission)
        .filter(
            Submission.round_id == round_id,
            Submission.user_id == user_id,
            Submission.state.in_(["ACCEPTED", "LOCKED"]),
        )
        .order_by(Submission.ordinal.asc())
        .all()
    )


def next_order_seq(session: Session, round_id: int, user_id: int) -> int:
    existing = (
        session.query(Order)
        .filter(Order.round_id == round_id, Order.user_id == user_id)
        .count()
    )
    return existing + 1


def find_reusable_order(session: Session, round_id: int, user_id: int) -> Order | None:
    """An OPEN order for this (round, user) is the SAME order cycling back
    from a reopen (state_machine.order_edit_reopens), not a new charge -- see
    the state diagram in portal_implementation.md §7. A genuinely new Order
    (seq incremented) is only created when there is no OPEN order to reuse,
    which happens on a user's first confirm, or when adding a further list
    after an earlier order has already been PAID/FROZEN/etc (never mutated).
    """
    return (
        session.query(Order)
        .filter(Order.round_id == round_id, Order.user_id == user_id, Order.state == "OPEN")
        .order_by(Order.seq.desc())
        .first()
    )


def confirm_order(session: Session, round_: Round, user: User) -> Order:
    """The 'Kostenübersicht bestätigen' action: locks every ACCEPTED
    submission, snapshots the price, and returns a LOCKED order with its
    reference code. Raises NoAcceptedSubmissions if there is nothing to
    charge for -- a route should not let a user reach this with an empty list.
    """
    submissions = [s for s in accepted_submissions(session, round_.id, user.id) if s.state == "ACCEPTED"]
    if not submissions:
        raise NoAcceptedSubmissions("no accepted submissions to lock into an order")

    rates = round_rates(round_)
    charges = [_charge_for(s) for s in submissions]
    breakdown = compute_breakdown(charges, rates)

    order = find_reusable_order(session, round_.id, user.id)
    if order is None:
        reference_code = make_reference_code(round_.code, user.person_slug, generate_readable_code(4))
        order = Order(
            round_id=round_.id, user_id=user.id,
            seq=next_order_seq(session, round_.id, user.id),
            reference_code=reference_code,
        )
        session.add(order)
        session.flush()
    else:
        # Reusing the order this list was already locked into before an edit
        # reopened it -- keep the reference code stable (the user may already
        # have started typing it into PayPal) and drop the stale links from
        # the previous lock cycle rather than let them accumulate.
        reference_code = order.reference_code
        session.query(OrderSubmission).filter(OrderSubmission.order_id == order.id).delete()
        session.flush()

    snapshot = PriceSnapshot(
        order_id=order.id,
        cost_per_card_cents=rates.cost_per_card_cents,
        cost_per_list_cents=rates.cost_per_list_cents,
        processing_charge_cents=rates.processing_charge_cents,
        n_cards=breakdown.n_cards,
        n_lists=breakdown.n_lists,
        total_cents=breakdown.total_cents,
        breakdown_json=json.dumps(breakdown.to_breakdown_dict()),
    )
    session.add(snapshot)
    session.flush()

    state_machine.lock_order(order, price_snapshot_id=snapshot.id, reference_code=reference_code)

    for sub, charge in zip(submissions, charges):
        state_machine.lock_for_order(sub)
        session.add(OrderSubmission(
            order_id=order.id,
            submission_id=sub.id,
            submission_version_id=sub.accepted_version_id,
            n_cards=charge.n_cards,
        ))

    return order
