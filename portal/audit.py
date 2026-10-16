"""A single place to write AuditLog rows, so every route logs consistently
instead of each handler inventing its own shape.
"""
import hashlib
import json

from sqlalchemy.orm import Session

from .models import AuditLog, User


def hash_ip(ip: str | None) -> str | None:
    """IPs are hashed at rest (portal_implementation.md §16 logging
    discipline) -- enough to correlate abuse without storing a raw IP."""
    if not ip:
        return None
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def record(
    session: Session,
    action: str,
    subject_type: str,
    subject_id: str,
    actor: User | None = None,
    actor_kind: str = "SYSTEM",
    detail: dict | None = None,
    ip: str | None = None,
) -> None:
    if actor is not None and actor_kind == "SYSTEM":
        # Caller passed a real actor but didn't say ADMIN/USER -- infer it
        # rather than silently mislabelling every action as SYSTEM.
        actor_kind = "ADMIN" if actor.is_admin else "USER"

    session.add(AuditLog(
        actor_user_id=actor.id if actor else None,
        actor_kind=actor_kind,
        action=action,
        subject_type=subject_type,
        subject_id=str(subject_id),
        detail_json=json.dumps(detail) if detail else None,
        ip_hash=hash_ip(ip),
    ))
