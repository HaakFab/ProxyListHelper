"""Submit, check, accept, edit and withdraw decklists."""
import hashlib
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import jobs, ratelimit, state_machine
from ..auth import require_user
from ..checking import SubmissionTooLarge, normalize_submitted_text
from ..csrf import verify_csrf
from ..db import get_session
from ..deps import flash, render
from ..models import Submission, SubmissionVersion, User
from ..rounds_service import get_open_round

router = APIRouter(prefix="/lists")


def _get_owned_submission(session: Session, submission_id: int, user: User) -> Submission:
    sub = session.get(Submission, submission_id)
    if sub is None or (sub.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Liste nicht gefunden")
    return sub


def _next_ordinal(session: Session, round_id: int, user_id: int) -> int:
    existing = (
        session.query(Submission)
        .filter(Submission.round_id == round_id, Submission.user_id == user_id)
        .count()
    )
    return existing + 1


@router.get("/new")
def new_list_form(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    round_ = get_open_round(session)
    if round_ is None:
        return render(request, "list_new.html", {
            "round": None,
            "flashes": [flash("error", "Derzeit läuft keine Sammelbestellung.")],
        }, user=user)
    return render(request, "list_new.html", {"round": round_}, user=user)


@router.post("/new")
def new_list_submit(
    request: Request,
    title: str = Form(...),
    raw_text: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    round_ = get_open_round(session)
    if round_ is None:
        return render(request, "list_new.html", {
            "round": None,
            "flashes": [flash("error", "Derzeit läuft keine Sammelbestellung.")],
        }, user=user, status_code=400)

    try:
        ratelimit.check_and_consume(session, "submission_create", str(user.id))
    except ratelimit.RateLimitExceeded:
        return render(request, "list_new.html", {
            "round": round_,
            "flashes": [flash("error", "Zu viele Einreichungen. Bitte versuche es später erneut.")],
        }, user=user, status_code=429)

    try:
        text = normalize_submitted_text(raw_text)
    except SubmissionTooLarge as ex:
        return render(request, "list_new.html", {
            "round": round_,
            "flashes": [flash("error", f"Liste zu groß: {ex}")],
            "title": title, "raw_text": raw_text,
        }, user=user, status_code=400)

    if not title.strip():
        return render(request, "list_new.html", {
            "round": round_,
            "flashes": [flash("error", "Bitte gib einen Titel für deine Liste an.")],
            "raw_text": raw_text,
        }, user=user, status_code=400)

    submission = Submission(
        round_id=round_.id, user_id=user.id, title=title.strip(),
        ordinal=_next_ordinal(session, round_.id, user.id), state="DRAFT",
    )
    session.add(submission)
    session.flush()

    version = SubmissionVersion(
        submission_id=submission.id, version_no=1, raw_text=text, source_kind="PASTE",
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    session.add(version)
    session.flush()

    state_machine.attach_new_version(submission, version.id)
    state_machine.start_checking(submission)
    jobs.enqueue_check(session, version.id)

    return RedirectResponse(f"/lists/{submission.id}", status_code=303)


@router.get("/{submission_id}")
def list_detail(
    submission_id: int, request: Request,
    user: User = Depends(require_user), session: Session = Depends(get_session),
):
    sub = _get_owned_submission(session, submission_id, user)
    version = sub.current_version
    check_run = version.check_run if version else None
    audit = json.loads(check_run.audit_json) if check_run and check_run.audit_json else None

    flashes = []
    if request.query_params.get("rate_limited"):
        flashes.append(flash("error", "Zu viele Änderungen. Bitte versuche es in einer Stunde erneut."))

    return render(request, "list_detail.html", {
        "submission": sub,
        "version": version,
        "check_run": check_run,
        "audit": audit,
        "flashes": flashes,
    }, user=user)


@router.post("/{submission_id}/accept")
def list_accept(
    submission_id: int,
    user: User = Depends(require_user), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    sub = _get_owned_submission(session, submission_id, user)
    if sub.current_version is None:
        raise HTTPException(status_code=400, detail="Keine Version zum Bestätigen vorhanden")
    state_machine.accept(sub, sub.current_version_id)
    return RedirectResponse(f"/lists/{submission_id}", status_code=303)


@router.post("/{submission_id}/edit")
def list_edit(
    submission_id: int,
    raw_text: str = Form(...),
    user: User = Depends(require_user), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    sub = _get_owned_submission(session, submission_id, user)

    try:
        ratelimit.check_and_consume(session, "recheck", str(user.id))
    except ratelimit.RateLimitExceeded:
        return RedirectResponse(f"/lists/{submission_id}?rate_limited=1", status_code=303)

    try:
        text = normalize_submitted_text(raw_text)
    except SubmissionTooLarge:
        raise HTTPException(status_code=400, detail="Liste zu groß")

    next_version_no = (
        session.query(SubmissionVersion)
        .filter(SubmissionVersion.submission_id == sub.id)
        .count()
    ) + 1
    version = SubmissionVersion(
        submission_id=sub.id, version_no=next_version_no, raw_text=text, source_kind="PASTE",
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    session.add(version)
    session.flush()

    state_machine.edit(session, sub)
    state_machine.attach_new_version(sub, version.id)
    jobs.enqueue_check(session, version.id)

    return RedirectResponse(f"/lists/{submission_id}", status_code=303)


@router.post("/{submission_id}/drop-card")
def list_drop_card(
    submission_id: int,
    line_number: int = Form(...),
    user: User = Depends(require_user), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    """Removes one offending line from the current version and re-checks --
    the 'RED lists: user can fix or drop the offending card' decision."""
    sub = _get_owned_submission(session, submission_id, user)
    current = sub.current_version
    if current is None:
        raise HTTPException(status_code=400, detail="Keine Version vorhanden")

    lines = current.raw_text.splitlines()
    remaining = [line for idx, line in enumerate(lines, start=1) if idx != line_number]
    text = "\n".join(remaining) + ("\n" if remaining else "")

    next_version_no = (
        session.query(SubmissionVersion)
        .filter(SubmissionVersion.submission_id == sub.id)
        .count()
    ) + 1
    version = SubmissionVersion(
        submission_id=sub.id, version_no=next_version_no, raw_text=text, source_kind="PASTE",
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    session.add(version)
    session.flush()

    state_machine.edit(session, sub)
    state_machine.attach_new_version(sub, version.id)
    jobs.enqueue_check(session, version.id)

    return RedirectResponse(f"/lists/{submission_id}", status_code=303)


@router.post("/{submission_id}/retry-check")
def list_retry_check(
    submission_id: int,
    user: User = Depends(require_user), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    """CHECK_FAILED means Scryfall was unreachable, not that the list itself
    is wrong -- re-run the same version rather than asking the user to
    retype anything."""
    sub = _get_owned_submission(session, submission_id, user)
    if sub.state != "CHECK_FAILED" or sub.current_version_id is None:
        raise HTTPException(status_code=400, detail="Nichts zum Wiederholen vorhanden")
    state_machine.start_checking(sub)
    jobs.enqueue_check(session, sub.current_version_id)
    return RedirectResponse(f"/lists/{submission_id}", status_code=303)


@router.post("/{submission_id}/withdraw")
def list_withdraw(
    submission_id: int,
    user: User = Depends(require_user), session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    sub = _get_owned_submission(session, submission_id, user)
    state_machine.withdraw(sub)
    return RedirectResponse("/", status_code=303)
