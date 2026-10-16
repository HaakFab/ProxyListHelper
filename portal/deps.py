"""Shared FastAPI wiring: the Jinja2 environment and a render() helper that
gives every page consistent CSRF/user/flash context without each route
repeating that boilerplate.
"""
import os

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from . import csrf
from .models import User
from .pricing import format_cents_eur
from .settings import cookie_secure

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.filters["eur"] = format_cents_eur


def _input_cents(cents) -> str:
    """For an HTML <input value="..."> bound to a nullable cents column.

    Jinja's `default` filter only substitutes on Undefined, not on Python
    None, so `{{ round.cost_per_card_cents }}` would render the literal text
    "None" into the field. A plain `or ''` fallback is just as wrong the
    other way: 0 is a legitimate, common rate (the default processing fee)
    and `0 or ''` discards it. This is the one correct check: only a real
    None becomes blank; 0 stays 0.
    """
    return "" if cents is None else str(cents)


templates.env.filters["input_cents"] = _input_cents

# Submission/order state -> (German label, badge colour). Centralised here so
# every template (home, list detail, admin) agrees on the same wording.
_SUBMISSION_LABELS = {
    "DRAFT": ("Entwurf", "muted"),
    "CHECKING": ("wird geprüft …", "muted"),
    "CHECK_FAILED": ("Prüfung fehlgeschlagen", "red"),
    "CLEAN": ("bereit", "green"),
    "NEEDS_REVIEW": ("Korrekturen vorgeschlagen", "yellow"),
    "NEEDS_FIXES": ("Korrektur nötig", "red"),
    "ACCEPTED": ("bestätigt", "green"),
    "LOCKED": ("in Bestellung", "green"),
    "FROZEN": ("bezahlt, eingefroren", "green"),
    "IN_PRINT_RUN": ("im Druck", "green"),
    "DONE": ("fertig", "green"),
    "WITHDRAWN": ("zurückgezogen", "muted"),
}

_ORDER_LABELS = {
    "OPEN": ("offen", "muted"),
    "LOCKED": ("Zahlung ausstehend", "yellow"),
    "PARTIALLY_PAID": ("teilweise bezahlt", "yellow"),
    "PAID": ("bezahlt", "green"),
    "FROZEN": ("bezahlt, eingefroren", "green"),
    "IN_PRINT_RUN": ("im Druck", "green"),
    "COMPLETED": ("abgeschlossen", "green"),
    "REFUNDED": ("erstattet", "muted"),
    "CANCELLED": ("storniert", "red"),
}


def _state_label(state: str, table: dict) -> str:
    return table.get(state, (state, "muted"))[0]


def _state_badge(state: str, table: dict) -> str:
    return table.get(state, (state, "muted"))[1]


templates.env.filters["submission_label"] = lambda s: _state_label(s, _SUBMISSION_LABELS)
templates.env.filters["submission_badge"] = lambda s: _state_badge(s, _SUBMISSION_LABELS)
templates.env.filters["order_label"] = lambda s: _state_label(s, _ORDER_LABELS)
templates.env.filters["order_badge"] = lambda s: _state_badge(s, _ORDER_LABELS)


def render(
    request: Request,
    name: str,
    context: dict | None = None,
    user: User | None = None,
    flashes: list[dict] | None = None,
    status_code: int = 200,
) -> Response:
    """The one place every GET/POST-with-errors handler should render a page.

    Ensures every template gets `current_user`, `csrf_token` and `flashes`
    without the route author remembering to pass them each time, and makes
    sure the CSRF cookie a template's hidden field references is actually set.
    """
    ctx = dict(context or {})
    ctx["current_user"] = user
    ctx.setdefault("flashes", flashes or [])
    csrf_token = csrf.get_or_create_csrf_token(request)
    ctx["csrf_token"] = csrf_token

    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    response.set_cookie(
        csrf.CSRF_COOKIE, csrf_token, httponly=False, samesite="lax", secure=cookie_secure(),
        max_age=60 * 60 * 24 * 7,
    )
    return response


def flash(kind: str, text: str) -> dict:
    return {"kind": kind, "text": text}
