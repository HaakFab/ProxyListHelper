"""Two independent auth layers (portal_implementation.md §11):

  1. The invite GATE -- one shared code, gates the entire site before anything
     renders. This is the "password protection" the project owner asked for,
     so the site doesn't read as a public product.
  2. The SESSION -- a personal login, established by redeeming a personal
     invite link (Phase 1) and from then on a signed session cookie.

They are deliberately separate cookies so either can be revoked without
touching the other, and so gate failures and login failures get independent
rate limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import security
from .db import get_session
from .models import LoginToken, Session as SessionModel, Setting, User, utcnow

GATE_COOKIE = "gate"
SESSION_COOKIE = "session"

GATE_TTL_DAYS = 30
SESSION_TTL_DAYS = 60
INVITE_LINK_TTL_DAYS = 14


# --- the invite gate ---------------------------------------------------------

def get_invite_code_hash(session: Session) -> str | None:
    row = session.get(Setting, "invite_code_hash")
    return row.value if row else None


def verify_gate_code(session: Session, code: str) -> bool:
    hashed = get_invite_code_hash(session)
    if not hashed:
        return False
    return security.verify_secret(code.strip(), hashed)


def gate_cookie_value() -> str:
    """A raw token to set as the gate cookie. Not tied to a specific invite
    code hash, so rotating the invite code doesn't need to invalidate every
    browser's gate cookie -- the marker just means "passed the gate once"."""
    return security.generate_token()


def require_gate(request: Request) -> None:
    """FastAPI dependency: raises 307 (handled by the route) if the gate
    cookie is missing. The actual code check happens once, on POST /gate;
    after that this dependency only checks the cookie exists."""
    if not request.cookies.get(GATE_COOKIE):
        raise GateRequired()


class GateRequired(Exception):
    """Distinct from HTTPException so middleware can redirect to /gate
    instead of rendering a bare 401."""


# --- personal sessions --------------------------------------------------------

def start_session(session: Session, user: User) -> str:
    """Creates a Session row for `user` and returns the raw cookie value.
    Shared by both login paths (link redemption and typed access code)."""
    user.last_seen_at = utcnow()
    raw_session = security.generate_token()
    session.add(SessionModel(
        user_id=user.id,
        token_hash=security.hash_token(raw_session),
        expires_at=utcnow() + timedelta(days=SESSION_TTL_DAYS),
    ))
    return raw_session


def login_with_access_code(session: Session, person_slug: str, raw_code: str) -> LoginResult | None:
    """The typed fallback for 'I lost my link' -- every user has a personal
    access code (see User.access_code_hash) independent of the invite links
    issued in portal/cli.py or by an admin. This is also how the bootstrap
    admin account logs in: its access code is set from ADMIN_PASSWORD.
    """
    user = session.query(User).filter(User.person_slug == person_slug).one_or_none()
    if user is None or user.is_blocked:
        return None
    if not security.verify_secret(raw_code, user.access_code_hash):
        return None
    return LoginResult(user=user, raw_session_token=start_session(session, user))


def issue_login_token(session: Session, user: User, purpose: str = "INVITE") -> str:
    """Creates a LoginToken row and returns the RAW token (only ever shown
    once, to the admin issuing the link -- the DB stores just its hash)."""
    raw = security.generate_token()
    session.add(LoginToken(
        user_id=user.id,
        token_hash=security.hash_token(raw),
        purpose=purpose,
        expires_at=utcnow() + timedelta(days=INVITE_LINK_TTL_DAYS),
    ))
    return raw


@dataclass
class LoginResult:
    user: User
    raw_session_token: str


def redeem_login_token(session: Session, raw_token: str) -> LoginResult | None:
    """Consumes a login/invite token and starts a new session. Returns None
    for any invalid/expired/already-used token -- callers should show one
    generic error, not distinguish the reasons (that would help an attacker
    enumerate valid-but-expired tokens)."""
    token_hash = security.hash_token(raw_token)
    token = (
        session.query(LoginToken)
        .filter(LoginToken.token_hash == token_hash)
        .one_or_none()
    )
    if token is None or token.used_at is not None or token.expires_at < utcnow():
        return None

    token.used_at = utcnow()
    user = session.get(User, token.user_id)
    if user is None or user.is_blocked:
        return None

    return LoginResult(user=user, raw_session_token=start_session(session, user))


def get_current_user(
    request: Request,
    session: Session = Depends(get_session),
) -> User | None:
    """FastAPI dependency. None means "not logged in" -- routes that require
    a user should depend on require_user instead, which turns None into a
    redirect."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    token_hash = security.hash_token(raw)
    row = (
        session.query(SessionModel)
        .filter(SessionModel.token_hash == token_hash)
        .one_or_none()
    )
    if row is None or row.revoked_at is not None or row.expires_at < utcnow():
        return None
    user = session.get(User, row.user_id)
    if user is None or user.is_blocked:
        return None
    # Sliding expiry: an active session keeps renewing its window rather than
    # expiring 60 days after login regardless of use.
    row.last_seen_at = utcnow()
    row.expires_at = utcnow() + timedelta(days=SESSION_TTL_DAYS)
    return user


class LoginRequired(Exception):
    """Raised by require_user; an exception handler turns this into a
    redirect to the gate/login page rather than a bare 401 page."""


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def revoke_session(session: Session, raw_token: str) -> None:
    token_hash = security.hash_token(raw_token)
    row = session.query(SessionModel).filter(SessionModel.token_hash == token_hash).one_or_none()
    if row is not None:
        row.revoked_at = utcnow()


def revoke_all_sessions(session: Session, user: User) -> None:
    """'Log out everywhere' -- portal_implementation.md §11."""
    for row in session.query(SessionModel).filter(SessionModel.user_id == user.id, SessionModel.revoked_at.is_(None)):
        row.revoked_at = utcnow()


# --- bearer auth for the Phase-2 worker API ----------------------------------

def verify_worker_token(provided: str, configured: str) -> bool:
    return security.constant_time_equals(provided, configured)
