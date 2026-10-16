"""Confirming a cost share and marking it as paid (from the user's side)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import audit as audit_log
from ..auth import require_user
from ..csrf import verify_csrf
from ..db import get_session
from ..deps import flash, render
from ..models import Order, User
from ..orders_service import NoAcceptedSubmissions, confirm_order
from ..rounds_service import get_open_round

router = APIRouter(prefix="/order")


def _current_order(session: Session, round_id: int, user_id: int) -> Order | None:
    return (
        session.query(Order)
        .filter(Order.round_id == round_id, Order.user_id == user_id)
        .order_by(Order.seq.desc())
        .first()
    )


@router.post("/confirm")
def order_confirm(
    request: Request,
    user: User = Depends(require_user), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    round_ = get_open_round(session)
    if round_ is None:
        return RedirectResponse("/", status_code=303)

    try:
        order = confirm_order(session, round_, user)
    except NoAcceptedSubmissions:
        return RedirectResponse("/", status_code=303)

    audit_log.record(
        session, action="order.confirmed", subject_type="Order", subject_id=order.id,
        actor=user, ip=request.client.host if request.client else None,
    )
    return RedirectResponse("/order", status_code=303)


@router.get("")
def order_detail(
    request: Request,
    user: User = Depends(require_user), session: Session = Depends(get_session),
):
    round_ = get_open_round(session)
    order = _current_order(session, round_.id, user.id) if round_ else None
    if order is None:
        return RedirectResponse("/", status_code=303)

    flashes = []
    if request.query_params.get("sent"):
        flashes.append(flash("info", "Danke! Wir prüfen den Zahlungseingang und bestätigen ihn hier."))

    return render(request, "order.html", {
        "order": order,
        "snapshot": order.price_snapshot,
        "paypal_handle": _paypal_handle(session),
        "flashes": flashes,
    }, user=user)


def _paypal_handle(session: Session) -> str:
    from ..models import Setting
    row = session.get(Setting, "paypal_me_handle")
    return row.value if row and row.value else ""


@router.post("/mark-sent")
def order_mark_sent(
    request: Request,
    user: User = Depends(require_user), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    round_ = get_open_round(session)
    order = _current_order(session, round_.id, user.id) if round_ else None
    if order is None or order.state != "LOCKED":
        return RedirectResponse("/order", status_code=303)

    audit_log.record(
        session, action="order.marked_sent", subject_type="Order", subject_id=order.id,
        actor=user, ip=request.client.host if request.client else None,
    )
    return RedirectResponse("/order?sent=1", status_code=303)
