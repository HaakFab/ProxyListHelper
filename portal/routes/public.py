"""The invite gate, login, logout, privacy notice, and the home dashboard."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import auth, ratelimit
from ..csrf import verify_csrf
from ..db import get_session
from ..deps import flash, render
from ..settings import cookie_secure
from ..models import Order, Setting, Submission
from ..orders_service import accepted_submissions
from ..rounds_service import get_open_round

router = APIRouter()


@router.get("/gate")
def gate_form(request: Request):
    if request.cookies.get(auth.GATE_COOKIE):
        return RedirectResponse("/", status_code=303)
    return render(request, "gate.html")


@router.post("/gate")
def gate_submit(
    request: Request,
    code: str = Form(...),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    client_ip = request.client.host if request.client else "unknown"
    try:
        ratelimit.check_and_consume(session, "gate", client_ip)
    except ratelimit.RateLimitExceeded:
        return render(
            request, "gate.html",
            {"flashes": [flash("error", "Zu viele Versuche. Bitte versuche es später erneut.")]},
            status_code=429,
        )

    if not auth.verify_gate_code(session, code):
        return render(
            request, "gate.html",
            {"flashes": [flash("error", "Falscher Zugangscode.")]},
            status_code=403,
        )

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.GATE_COOKIE, auth.gate_cookie_value(),
        httponly=True, samesite="lax", secure=cookie_secure(),
        max_age=60 * 60 * 24 * auth.GATE_TTL_DAYS,
    )
    return response


@router.get("/login")
def login_form(request: Request):
    if not request.cookies.get(auth.GATE_COOKIE):
        return RedirectResponse("/gate", status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login_submit(
    request: Request,
    person_slug: str = Form(...),
    access_code: str = Form(...),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    client_ip = request.client.host if request.client else "unknown"
    try:
        ratelimit.check_and_consume(session, "gate", f"login:{client_ip}")
    except ratelimit.RateLimitExceeded:
        return render(
            request, "login.html",
            {"flashes": [flash("error", "Zu viele Versuche. Bitte versuche es später erneut.")]},
            status_code=429,
        )

    result = auth.login_with_access_code(session, person_slug.strip(), access_code)
    if result is None:
        return render(
            request, "login.html",
            {"flashes": [flash("error", "Name oder Zugangscode falsch.")]},
            status_code=403,
        )

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, result.raw_session_token,
        httponly=True, samesite="lax", secure=cookie_secure(),
        max_age=60 * 60 * 24 * auth.SESSION_TTL_DAYS,
    )
    return response


@router.get("/login/{token}")
def login_with_token(token: str, request: Request, session: Session = Depends(get_session)):
    result = auth.redeem_login_token(session, token)
    if result is None:
        return render(request, "login_invalid.html", status_code=403)

    response = render(request, "login_success.html", {"user": result.user}, user=result.user)
    response.set_cookie(
        auth.SESSION_COOKIE, result.raw_session_token,
        httponly=True, samesite="lax", secure=cookie_secure(),
        max_age=60 * 60 * 24 * auth.SESSION_TTL_DAYS,
    )
    # Anyone following a login link has, by definition, also passed the gate
    # in spirit (the link itself is not public) -- but the gate is a
    # site-wide check independent of identity, so set it too.
    if not request.cookies.get(auth.GATE_COOKIE):
        response.set_cookie(
            auth.GATE_COOKIE, auth.gate_cookie_value(),
            httponly=True, samesite="lax", secure=cookie_secure(),
            max_age=60 * 60 * 24 * auth.GATE_TTL_DAYS,
        )
    return response


@router.post("/logout")
def logout(
    request: Request,
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    raw = request.cookies.get(auth.SESSION_COOKIE)
    if raw:
        auth.revoke_session(session, raw)
    response = RedirectResponse("/gate", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@router.get("/datenschutz")
def datenschutz(request: Request, session: Session = Depends(get_session)):
    version_row = session.get(Setting, "privacy_version")
    return render(request, "datenschutz.html", {
        "controller_name": "der Organisator dieser Sammelbestellung",
        "controller_contact": "siehe Gruppenchat",
        "privacy_version": version_row.value if version_row else "1.0",
    })


@router.get("/")
def home(
    request: Request,
    user=Depends(auth.get_current_user),
    session: Session = Depends(get_session),
):
    if not request.cookies.get(auth.GATE_COOKIE):
        return RedirectResponse("/gate", status_code=303)
    if user is None:
        return render(request, "home_logged_out.html")

    round_ = get_open_round(session)
    submissions = []
    order = None
    if round_ is not None:
        submissions = (
            session.query(Submission)
            .filter(Submission.round_id == round_.id, Submission.user_id == user.id)
            .order_by(Submission.ordinal.asc())
            .all()
        )
        accepted = [s for s in accepted_submissions(session, round_.id, user.id) if s.state == "ACCEPTED"]
        order = (
            session.query(Order)
            .filter(Order.round_id == round_.id, Order.user_id == user.id)
            .order_by(Order.seq.desc())
            .first()
        )
    else:
        accepted = []

    return render(request, "home.html", {
        "round": round_,
        "submissions": submissions,
        "has_acceptable": bool(accepted),
        "order": order,
    }, user=user)
