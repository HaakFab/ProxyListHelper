"""Bootstrap configuration, read from the environment / .env file.

Deliberately small: this holds only what's needed to *start the process* --
secrets, the database location, mail backend selection. Everything an admin
tunes at runtime (prices, PayPal handle, invite code, retention window) lives
in the `setting` DB table instead (see portal/models.py Setting), because it
must be editable without a redeploy and some of it (prices) must be
snapshotted onto rounds and orders rather than read live.
"""
import secrets as _secrets
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULTS = {"", "changeme", "change-me", "insecure", "secret", "default"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- secrets -----------------------------------------------------------
    secret_key: str = ""
    worker_token: str = ""
    admin_password: str = ""
    invite_code: str = ""

    # --- money / identity defaults, only used to SEED the settings table ---
    paypal_me_handle: str = ""

    # --- infrastructure ------------------------------------------------------
    portal_domain: str = "localhost"
    database_url: str = "sqlite:///./data/portal.db"
    scryfall_cache_dir: str = "./data/scryfall_web_cache"  # fallback if not using DB cache
    scryfall_cache_ttl_days: int = 30
    retention_days: int = 90

    # --- mail (Phase 3; "none" keeps the portal fully functional) -----------
    mail_backend: str = "none"  # none | smtp | brevo
    mail_from: str = ""
    mail_api_key: str = ""

    debug: bool = False

    @field_validator("secret_key")
    @classmethod
    def _secret_key_must_be_real(cls, v: str) -> str:
        if v.strip().lower() in _INSECURE_DEFAULTS or len(v) < 32:
            raise ValueError(
                "SECRET_KEY is empty, a placeholder, or too short (need >= 32 chars). "
                "A weak signing key is a silent authentication bypass -- refusing to start. "
                "Generate one with: openssl rand -base64 32"
            )
        return v

    @field_validator("worker_token")
    @classmethod
    def _worker_token_must_be_real(cls, v: str) -> str:
        if v.strip().lower() in _INSECURE_DEFAULTS or len(v) < 24:
            raise ValueError(
                "WORKER_TOKEN is empty, a placeholder, or too short. "
                "Generate one with: openssl rand -base64 32"
            )
        return v

    @field_validator("invite_code")
    @classmethod
    def _invite_code_must_be_real(cls, v: str) -> str:
        if v.strip().lower() in _INSECURE_DEFAULTS:
            raise ValueError(
                "INVITE_CODE is empty or a placeholder. Set a real shared code -- "
                "this is the only thing gating the whole site."
            )
        return v


def generate_dev_secret() -> str:
    """A throwaway secret for local dev/tests only -- never for a real deployment."""
    return _secrets.token_urlsafe(32)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def cookie_secure() -> bool:
    """Whether auth/CSRF cookies should carry the Secure flag.

    True (the only sane choice) whenever the site is reachable over HTTPS --
    which in production it always is, behind Caddy (see deploy/Caddyfile).
    False only in debug mode, so a plain `http://localhost:8000` dev/test run
    (bypassing Caddy entirely) still works in a REAL browser: unlike curl,
    a browser silently refuses to store or resend a Secure cookie over plain
    HTTP, which would otherwise break the gate/session cookie in exactly that
    setup -- see deploy/SETUP.md's "test locally first" section.
    """
    return not get_settings().debug
