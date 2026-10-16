"""The admin surface (portal_implementation.md §14). Every mutation here is
audit-logged -- see the `audit_log.record(...)` call at the end of each
handler.
"""
import csv
import io
import zipfile
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import audit as audit_log
from .. import security, state_machine
from ..auth import issue_login_token, require_admin
from ..csrf import verify_csrf
from ..db import get_session
from ..deps import flash, render
from ..models import Order, PaymentRecord, Round, Setting, Submission, User
from ..models import utcnow as models_utcnow
from ..pricing import balance_cents
from ..slugs import make_unique_person_slug

router = APIRouter(prefix="/admin")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# --- dashboard ---------------------------------------------------------------

@router.get("")
def dashboard(request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    round_ = session.query(Round).order_by(Round.id.desc()).first()
    submission_counts = {}
    orders = []
    if round_ is not None:
        rows = (
            session.query(Submission.state, func.count(Submission.id))
            .filter(Submission.round_id == round_.id)
            .group_by(Submission.state)
            .all()
        )
        submission_counts = dict(rows)
        orders = session.query(Order).filter(Order.round_id == round_.id).all()

    expected = sum(o.price_snapshot.total_cents for o in orders if o.price_snapshot)
    received = sum(o.total_paid_cents for o in orders)

    flashes = []
    if request.query_params.get("rates_missing"):
        flashes.append(flash("error", "Diese Runde hat noch keine Preise. Bitte zuerst speichern."))

    return render(request, "admin/dashboard.html", {
        "round": round_,
        "submission_counts": submission_counts,
        "orders": orders,
        "expected_cents": expected,
        "received_cents": received,
        # Pre-fills the "Neue Runde" form -- see round_create's docstring for
        # why these are only ever a starting point, never read again later.
        "default_rates": {
            "cost_per_card_cents": _setting(session, "cost_per_card_cents", "13"),
            "cost_per_list_cents": _setting(session, "cost_per_list_cents", "26"),
            "processing_charge_cents": _setting(session, "processing_charge_cents", "0"),
        },
        "flashes": flashes,
    }, user=admin)


# --- rounds --------------------------------------------------------------------

@router.post("/round/new")
def round_create(
    request: Request,
    code: str = Form(...), name: str = Form(...),
    cost_per_card_cents: int = Form(...), cost_per_list_cents: int = Form(...),
    processing_charge_cents: int = Form(...),
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    """Rates are set HERE, directly on the round, not read from Settings
    later at open time. This closes a real gap the old flow had: a DRAFT
    round used to carry no rates of its own and would silently pick up
    whatever Settings said at the moment someone clicked "Runde öffnen" --
    which could differ from what the admin saw when they created it, if
    Settings had been edited in between. Settings now only supplies the
    pre-filled defaults on this form (see admin/dashboard.html); once
    submitted, the round's price is its own, independent of Settings.
    """
    round_ = Round(
        code=code.strip(), name=name.strip(), state="DRAFT",
        cost_per_card_cents=cost_per_card_cents,
        cost_per_list_cents=cost_per_list_cents,
        processing_charge_cents=processing_charge_cents,
    )
    session.add(round_)
    audit_log.record(session, "round.created", "Round", code, actor=admin, ip=_client_ip(request), detail={
        "cost_per_card_cents": cost_per_card_cents,
        "cost_per_list_cents": cost_per_list_cents,
        "processing_charge_cents": processing_charge_cents,
    })
    return RedirectResponse("/admin", status_code=303)


@router.post("/round/{round_id}/update-rates")
def round_update_rates(
    round_id: int, request: Request,
    cost_per_card_cents: int = Form(...), cost_per_list_cents: int = Form(...),
    processing_charge_cents: int = Form(...),
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    """Adjust a round's rates before opening it. Refuses once the round has
    left DRAFT: an OPEN+ round may already have locked-in PriceSnapshots, and
    changing its rates here would silently disagree with prices already
    shown to people (the correct way to reprice after opening is per-order,
    via admin unlock + reconfirm, which is audited per order)."""
    round_ = session.get(Round, round_id)
    if round_ is None or round_.state != "DRAFT":
        return RedirectResponse("/admin", status_code=303)

    round_.cost_per_card_cents = cost_per_card_cents
    round_.cost_per_list_cents = cost_per_list_cents
    round_.processing_charge_cents = processing_charge_cents
    audit_log.record(session, "round.rates_updated", "Round", round_.id, actor=admin, ip=_client_ip(request), detail={
        "cost_per_card_cents": cost_per_card_cents,
        "cost_per_list_cents": cost_per_list_cents,
        "processing_charge_cents": processing_charge_cents,
    })
    return RedirectResponse("/admin", status_code=303)


@router.post("/round/{round_id}/open")
def round_open(
    round_id: int, request: Request,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    round_ = session.get(Round, round_id)
    if round_ is None:
        return RedirectResponse("/admin", status_code=303)
    if round_.cost_per_card_cents is None or round_.cost_per_list_cents is None \
            or round_.processing_charge_cents is None:
        # Shouldn't happen through this UI (round_create always sets them),
        # but never open a round with undefined pricing -- orders_service
        # would crash mid-confirm trying to multiply against None.
        return RedirectResponse("/admin?rates_missing=1", status_code=303)

    # Rates are already fixed (set at creation, adjustable only while DRAFT
    # via round_update_rates above) -- opening just flips the state.
    round_.state = "OPEN"
    round_.opens_at = models_utcnow()

    audit_log.record(session, "round.opened", "Round", round_.id, actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin", status_code=303)


@router.post("/round/{round_id}/close")
def round_close(
    round_id: int, request: Request,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    round_ = session.get(Round, round_id)
    if round_ is not None:
        round_.state = "CLOSED"
        round_.closes_at = models_utcnow()
        audit_log.record(session, "round.closed", "Round", round_.id, actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin", status_code=303)


@router.post("/round/{round_id}/complete")
def round_complete(
    round_id: int, request: Request,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    round_ = session.get(Round, round_id)
    if round_ is not None:
        round_.state = "COMPLETED"
        audit_log.record(session, "round.completed", "Round", round_.id, actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin", status_code=303)


# --- settings --------------------------------------------------------------------

def _setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def _set_setting(session: Session, key: str, value: str, admin: User) -> None:
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        session.add(row)
    row.value = value
    row.updated_by_user_id = admin.id


@router.get("/settings")
def settings_form(request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    keys = ["cost_per_card_cents", "cost_per_list_cents", "processing_charge_cents",
            "paypal_me_handle", "retention_days"]
    values = {k: _setting(session, k) for k in keys}
    return render(request, "admin/settings.html", {"values": values}, user=admin)


@router.post("/settings")
def settings_update(
    request: Request,
    cost_per_card_cents: int = Form(...), cost_per_list_cents: int = Form(...),
    processing_charge_cents: int = Form(...), paypal_me_handle: str = Form(...),
    retention_days: int = Form(...),
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    for key, value in [
        ("cost_per_card_cents", cost_per_card_cents),
        ("cost_per_list_cents", cost_per_list_cents),
        ("processing_charge_cents", processing_charge_cents),
        ("paypal_me_handle", paypal_me_handle.strip()),
        ("retention_days", retention_days),
    ]:
        _set_setting(session, key, str(value), admin)

    audit_log.record(session, "settings.updated", "Setting", "rates", actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/settings/rotate-invite-code")
def rotate_invite_code(
    request: Request, new_code: str = Form(...),
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    _set_setting(session, "invite_code_hash", security.hash_secret(new_code), admin)
    audit_log.record(session, "settings.invite_code_rotated", "Setting", "invite_code_hash",
                      actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin/settings", status_code=303)


# --- people ------------------------------------------------------------------

@router.get("/people")
def people_list(request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    people = session.query(User).order_by(User.display_name.asc()).all()
    return render(request, "admin/people.html", {"people": people}, user=admin)


@router.post("/people/new")
def people_create(
    request: Request, display_name: str = Form(...),
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    existing_slugs = {row.person_slug for row in session.query(User.person_slug).all()}
    slug = make_unique_person_slug(display_name.strip(), existing_slugs)

    user = User(
        display_name=display_name.strip(), person_slug=slug,
        access_code_hash=security.hash_secret(security.generate_readable_code(10)),
    )
    session.add(user)
    session.flush()

    raw_link_token = issue_login_token(session, user, purpose="INVITE")
    audit_log.record(session, "user.created", "User", user.id, actor=admin, ip=_client_ip(request))

    link_url = f"{request.base_url}login/{raw_link_token}"
    people = session.query(User).order_by(User.display_name.asc()).all()
    return render(request, "admin/people.html", {
        "people": people,
        "flashes": [flash("info", f"{user.display_name}: Link erstellt.")],
        "new_link": link_url, "new_link_for": user.display_name,
    }, user=admin)


@router.post("/people/{user_id}/new-link")
def people_new_link(
    user_id: int, request: Request,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    user = session.get(User, user_id)
    if user is None:
        return RedirectResponse("/admin/people", status_code=303)
    raw_link_token = issue_login_token(session, user, purpose="INVITE")
    audit_log.record(session, "user.link_issued", "User", user.id, actor=admin, ip=_client_ip(request))

    link_url = f"{request.base_url}login/{raw_link_token}"
    people = session.query(User).order_by(User.display_name.asc()).all()
    return render(request, "admin/people.html", {
        "people": people,
        "flashes": [flash("info", f"Neuer Link für {user.display_name} erstellt.")],
        "new_link": link_url, "new_link_for": user.display_name,
    }, user=admin)


@router.post("/people/{user_id}/block")
def people_block(
    user_id: int, request: Request,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    user = session.get(User, user_id)
    if user is not None:
        user.is_blocked = not user.is_blocked
        audit_log.record(session, "user.block_toggled", "User", user.id,
                          detail={"is_blocked": user.is_blocked}, actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin/people", status_code=303)


# --- orders (the money screen) --------------------------------------------------

@router.get("/orders")
def orders_list(request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    round_ = session.query(Round).order_by(Round.id.desc()).first()
    orders = []
    if round_ is not None:
        orders = (
            session.query(Order)
            .filter(Order.round_id == round_.id)
            .order_by(Order.id.asc())
            .all()
        )
    balances = {o.id: balance_cents(o.price_snapshot.total_cents, [p.amount_cents for p in o.payments])
                for o in orders if o.price_snapshot}
    return render(request, "admin/orders.html", {
        "round": round_, "orders": orders, "balances": balances, "today": date.today().isoformat(),
    }, user=admin)


@router.post("/orders/{order_id}/record-payment")
def order_record_payment(
    order_id: int, request: Request,
    amount_cents: int = Form(...), kind: str = Form("PAYPAL"),
    received_on: str = Form(...), note: str = Form(""),
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    order = session.get(Order, order_id)
    if order is None or order.price_snapshot is None:
        return RedirectResponse("/admin/orders", status_code=303)

    session.add(PaymentRecord(
        order_id=order.id, amount_cents=amount_cents, kind=kind,
        received_on=date.fromisoformat(received_on), note=note.strip() or None,
        recorded_by_user_id=admin.id,
    ))
    session.flush()

    total_paid = sum(p.amount_cents for p in order.payments)
    state_machine.record_payment_total(order, total_paid, order.price_snapshot.total_cents)

    audit_log.record(session, "payment.recorded", "Order", order.id,
                      detail={"amount_cents": amount_cents, "kind": kind}, actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin/orders", status_code=303)


@router.post("/orders/{order_id}/unlock")
def order_unlock(
    order_id: int, request: Request,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    order = session.get(Order, order_id)
    if order is not None:
        state_machine.admin_unlock_order(order)
        for link in order.submissions:
            sub = session.get(Submission, link.submission_id)
            if sub is not None and sub.state in ("LOCKED", "FROZEN"):
                state_machine.admin_unlock(sub)
        audit_log.record(session, "order.admin_unlocked", "Order", order.id, actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin/orders", status_code=303)


@router.post("/orders/{order_id}/freeze")
def order_freeze(
    order_id: int, request: Request,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    order = session.get(Order, order_id)
    if order is not None:
        state_machine.freeze_order(order)
        for link in order.submissions:
            sub = session.get(Submission, link.submission_id)
            if sub is not None and sub.state == "LOCKED":
                state_machine.freeze(sub)
        audit_log.record(session, "order.frozen", "Order", order.id, actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin/orders", status_code=303)


@router.post("/orders/{order_id}/cancel")
def order_cancel(
    order_id: int, request: Request, reason: str = Form(...),
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    order = session.get(Order, order_id)
    if order is not None:
        state_machine.cancel_order(order, reason)
        audit_log.record(session, "order.cancelled", "Order", order.id,
                          detail={"reason": reason}, actor=admin, ip=_client_ip(request))
    return RedirectResponse("/admin/orders", status_code=303)


# --- round export: the Phase-1 escape hatch --------------------------------------

@router.get("/round/{round_id}/export.zip")
def round_export(
    round_id: int,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
):
    """Every FROZEN (paid) submission's fixed text, named exactly as the
    Phase-2 worker would write it, plus a totals CSV -- until print_worker.py
    exists, this is how Fabian actually runs the pipeline: unzip into
    `ready/` and run moxfield_mpc_to_pdf.py --input-dir ready unchanged.
    """
    from ..slugs import worker_filename

    round_ = session.get(Round, round_id)
    submissions = (
        session.query(Submission)
        .filter(Submission.round_id == round_id, Submission.state.in_(["FROZEN", "LOCKED"]))
        .order_by(Submission.user_id.asc(), Submission.ordinal.asc())
        .all()
    ) if round_ else []

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        summary = io.StringIO()
        writer = csv.writer(summary)
        writer.writerow(["Person", "Liste", "Karten", "Status"])

        for sub in submissions:
            version = sub.accepted_version or sub.current_version
            if version is None or version.check_run is None:
                continue
            filename = worker_filename(sub.user.person_slug, sub.ordinal, sub.title)
            zf.writestr(f"ready/{filename}", version.check_run.fixed_text or version.raw_text)
            writer.writerow([sub.user.display_name, sub.title, version.check_run.total_cards, sub.state])

        zf.writestr("summary.csv", summary.getvalue())

    buffer.seek(0)
    return StreamingResponse(
        buffer, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=round-{round_id}-ready.zip"},
    )
