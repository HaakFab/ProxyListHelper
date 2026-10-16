"""Password/token hashing, random token generation, and constant-time compares.

Two different hashing needs, deliberately kept apart:
  - argon2id for anything a human types and re-enters (invite code, personal
    access code, admin password) -- slow by design, resists brute force.
  - sha256 for random high-entropy tokens the app itself generated (session
    cookies, login-link tokens, worker bearer token) -- these don't need a
    slow hash because they can't be guessed; sha256 just avoids storing the
    raw secret at rest so a DB leak doesn't hand out live sessions.
"""
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()

# Crockford base32 minus I, O, U (visually confusable) and 1, 0 (people
# transcribe these by hand into a PayPal note or an invite link).
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTVWXYZ23456789"


# --- human-entered secrets (argon2id) ---------------------------------------

def hash_secret(raw: str) -> str:
    return _ph.hash(raw)


def verify_secret(raw: str, hashed: str) -> bool:
    """True if `raw` matches `hashed`. Never raises on mismatch."""
    try:
        return _ph.verify(hashed, raw)
    except VerifyMismatchError:
        return False
    except Exception:
        # A corrupt/foreign hash format must fail closed, not raise past auth.
        return False


def needs_rehash(hashed: str) -> bool:
    """True if argon2 parameters have since been tightened and this hash is stale."""
    return _ph.check_needs_rehash(hashed)


# --- app-generated tokens (sha256 at rest) ----------------------------------

def generate_token(n_bytes: int = 32) -> str:
    """A raw, URL-safe token. This is what goes in a cookie or a link -- the
    database only ever sees its hash (see hash_token)."""
    return secrets.token_urlsafe(n_bytes)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --- human-facing codes (invite links, reference codes) ---------------------

def generate_readable_code(n_chars: int = 4) -> str:
    """Short, unambiguous code for things a person reads or types back:
    order reference codes, personal access codes. Not for anything that
    needs real cryptographic unguessability at small length -- see
    generate_token for that."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n_chars))
