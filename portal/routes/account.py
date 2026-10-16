"""Account settings and the GDPR self-service endpoints (export/delete)."""
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import audit as audit_log
from .. import ratelimit
from ..auth import GATE_COOKIE, SESSION_COOKIE, require_user, revoke_all_sessions
from ..csrf import verify_csrf
from ..db import get_session
from ..deps import flash, render
from ..models import (
    AuditLog,
    Order,
    PaymentRecord,
    PriceSnapshot,
    Submission,
    SubmissionVersion,
    User,
)

router = APIRouter(prefix="/account")


@router.get("")
def account_home(request: Request, user: User = Depends(require_user)):
    return render(request, "account.html", {"user": user}, user=user)


@router.post("")
def account_update(
    request: Request,
    email: str = Form(""),
    notify: bool = Form(False),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    user.email = email.strip() or None
    user.notify = bool(notify) and bool(user.email)
    return render(request, "account.html", {
        "user": user, "flashes": [flash("info", "Gespeichert.")],
    }, user=user)


@router.post("/logout-everywhere")
def account_logout_everywhere(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    revoke_all_sessions(session, user)
    response = RedirectResponse("/gate", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/export")
def account_export(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """A single JSON export of everything the portal holds about this user --
    portal_implementation.md §16's data-subject-access-request endpoint."""
    try:
        ratelimit.check_and_consume(session, "export", str(user.id))
    except ratelimit.RateLimitExceeded:
        return render(request, "account.html", {
            "user": user,
            "flashes": [flash("error", "Export bereits vor Kurzem angefordert. Bitte in einer Stunde erneut versuchen.")],
        }, user=user, status_code=429)

    submissions = session.query(Submission).filter(Submission.user_id == user.id).all()
    orders = session.query(Order).filter(Order.user_id == user.id).all()

    data = {
        "profile": {
            "display_name": user.display_name,
            "person_slug": user.person_slug,
            "email": user.email,
            "notify": user.notify,
            "is_admin": user.is_admin,
            "created_at": user.created_at.isoformat(),
            "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
        },
        "submissions": [
            {
                "id": s.id, "title": s.title, "state": s.state, "round_id": s.round_id,
                "versions": [
                    {
                        "version_no": v.version_no, "raw_text": v.raw_text,
                        "content_sha256": v.content_sha256, "created_at": v.created_at.isoformat(),
                        "check": ({
                            "traffic_light": v.check_run.traffic_light,
                            "total_cards": v.check_run.total_cards,
                            "fixed_text": v.check_run.fixed_text,
                        } if v.check_run else None),
                    }
                    for v in s.versions
                ],
            }
            for s in submissions
        ],
        "orders": [
            {
                "id": o.id, "seq": o.seq, "state": o.state, "reference_code": o.reference_code,
                "price_snapshot": (
                    json.loads(o.price_snapshot.breakdown_json) if o.price_snapshot else None
                ),
                "payments": [
                    {"amount_cents": p.amount_cents, "kind": p.kind, "received_on": p.received_on.isoformat()}
                    for p in o.payments
                ],
            }
            for o in orders
        ],
    }
    return JSONResponse(data)


@router.get("/delete")
def account_delete_confirm(request: Request, user: User = Depends(require_user)):
    return render(request, "account_delete.html", {"user": user}, user=user)


@router.post("/delete")
def account_delete(
    request: Request,
    confirm: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    if confirm.strip().lower() != "löschen":
        return render(request, "account_delete.html", {
            "user": user, "flashes": [flash("error", "Bitte 'löschen' zur Bestätigung eingeben.")],
        }, user=user, status_code=400)

    # Orders mid-print (FROZEN/IN_PRINT_RUN/COMPLETED) keep their amounts for
    # the round's bookkeeping but lose every identifying detail -- the UI on
    # account_delete.html says this up front, before the user confirms.
    active_orders = (
        session.query(Order)
        .filter(Order.user_id == user.id, Order.state.in_(["FROZEN", "IN_PRINT_RUN", "COMPLETED"]))
        .all()
    )
    pseudonym = f"deleted-user-{user.id}"
    for order in active_orders:
        order.user_id = user.id  # kept: FK must remain valid until the user row itself is gone

    audit_log.record(
        session, action="account.self_deleted", subject_type="User", subject_id=user.id,
        actor=user, ip=request.client.host if request.client else None,
    )

    if active_orders:
        # Can't delete the User row while a FROZEN/printed order still
        # references it (FK) -- anonymise instead of a hard delete for those.
        user.display_name = pseudonym
        user.email = None
        user.notify = False
        user.is_blocked = True
        revoke_all_sessions(session, user)
        response = RedirectResponse("/gate", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    revoke_all_sessions(session, user)
    session.delete(user)  # cascades: submissions, versions, check_runs, sessions, login_tokens, orders(none active)
    response = RedirectResponse("/gate", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(GATE_COOKIE)
    return response
