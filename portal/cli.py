"""One-shot administrative commands, run inside the container -- never as a
web route (portal_implementation.md §11: "Admin bootstrap via a one-shot CLI
... never a web route").

Usage:
    python -m portal.cli bootstrap
    python -m portal.cli promote <person_slug>
    python -m portal.cli rotate-invite-code
"""
import argparse
import sys

from . import security
from .db import init_engine, session_scope
from .models import Setting, User
from .settings import get_settings

DEFAULT_RATES = {
    "cost_per_card_cents": "13",
    "cost_per_list_cents": "26",
    "processing_charge_cents": "0",
}


def _ensure_setting(session, key: str, value: str) -> None:
    """Seeds a setting only if it doesn't already exist -- bootstrap must be
    safe to run again on an existing database (e.g. after a redeploy) without
    clobbering an admin's already-configured rates."""
    if session.get(Setting, key) is None:
        session.add(Setting(key=key, value=value))


def bootstrap() -> None:
    settings = get_settings()
    init_engine(settings.database_url)

    with session_scope() as session:
        for key, value in DEFAULT_RATES.items():
            _ensure_setting(session, key, value)
        _ensure_setting(session, "paypal_me_handle", settings.paypal_me_handle)
        _ensure_setting(session, "retention_days", str(settings.retention_days))
        _ensure_setting(session, "invite_code_hash", security.hash_secret(settings.invite_code))

        admin = session.query(User).filter(User.is_admin.is_(True)).first()
        if admin is None:
            admin = User(
                display_name="Admin",
                person_slug="Admin",
                access_code_hash=security.hash_secret(settings.admin_password),
                is_admin=True,
            )
            session.add(admin)
            print("Created admin account (person_slug='Admin'). "
                  "Log in with your ADMIN_PASSWORD as the access code.")
        else:
            print(f"Admin account already exists (person_slug={admin.person_slug!r}); "
                  "not touching its access code. Use 'promote'/'reset-admin-password' "
                  "if you need to change it.")

    print("Bootstrap complete.")


def promote(person_slug: str) -> None:
    settings = get_settings()
    init_engine(settings.database_url)
    with session_scope() as session:
        user = session.query(User).filter(User.person_slug == person_slug).one_or_none()
        if user is None:
            print(f"No user with person_slug={person_slug!r}", file=sys.stderr)
            sys.exit(1)
        user.is_admin = True
    print(f"{person_slug} is now an admin.")


def rotate_invite_code(new_code: str) -> None:
    settings = get_settings()
    init_engine(settings.database_url)
    with session_scope() as session:
        row = session.get(Setting, "invite_code_hash")
        hashed = security.hash_secret(new_code)
        if row is None:
            session.add(Setting(key="invite_code_hash", value=hashed))
        else:
            row.value = hashed
    print("Invite code rotated. Existing gate cookies remain valid (they only "
          "mark 'passed the gate once', not which code was used) -- rotating "
          "is for stopping NEW people from getting in with a leaked old code.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m portal.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap", help="Seed default settings and create the admin account")

    promote_parser = sub.add_parser("promote", help="Grant admin rights to an existing user")
    promote_parser.add_argument("person_slug")

    rotate_parser = sub.add_parser("rotate-invite-code", help="Change the shared gate code")
    rotate_parser.add_argument("new_code")

    args = parser.parse_args()

    if args.command == "bootstrap":
        bootstrap()
    elif args.command == "promote":
        promote(args.person_slug)
    elif args.command == "rotate-invite-code":
        rotate_invite_code(args.new_code)


if __name__ == "__main__":
    main()
