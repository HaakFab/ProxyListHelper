"""Double-submit CSRF protection for the server-rendered forms.

A random token is set as a cookie (not HttpOnly -- it must be readable so a
template can echo it into a hidden form field) and every POST must submit the
same value back in the form body. An attacker who can trigger a cross-site
POST cannot read the victim's cookie, so cannot supply a matching token.
"""
from fastapi import Form, HTTPException, Request

from . import security

CSRF_COOKIE = "csrf"


def get_or_create_csrf_token(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or security.generate_token(16)


def verify_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    cookie_value = request.cookies.get(CSRF_COOKIE)
    if not cookie_value or not security.constant_time_equals(cookie_value, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
