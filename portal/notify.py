"""Pluggable notification sender. Default backend is "none": the portal is
fully functional with zero mail configuration (login is link-based, not
email-based -- see portal/auth.py). This exists only for the OPT-IN "your
order is done" notification a user can enable if they gave an email address.

Phase 3 backends (not yet implemented): "smtp", "brevo". Both intentionally
excluded from Phase 1 -- see portal_implementation.md §16/Phase 3: a fresh
VPS has no sending reputation and Hetzner blocks outbound port 25 on new
accounts, so naive SMTP-from-the-VPS lands in spam. Do this properly later
with an EU transactional provider, or not at all.
"""
import logging

from .settings import Settings

logger = logging.getLogger("portal.notify")


class Notifier:
    def send(self, to_email: str, subject: str, body: str) -> None:
        raise NotImplementedError


class NoopNotifier(Notifier):
    """Logs instead of sending. This is the correct, supported default."""

    def send(self, to_email: str, subject: str, body: str) -> None:
        logger.info("notify(backend=none): would send to %s: %s", to_email, subject)


def get_notifier(settings: Settings) -> Notifier:
    if settings.mail_backend == "none":
        return NoopNotifier()
    raise NotImplementedError(
        f"mail_backend={settings.mail_backend!r} is not implemented yet (Phase 3). "
        "Set MAIL_BACKEND=none to keep the portal running without email."
    )
