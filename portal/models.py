"""SQLAlchemy models -- the full data model from portal_implementation.md §6.

Conventions used throughout:
  - All timestamps are UTC, stored as naive datetimes (SQLite has no tz type;
    every read/write goes through utcnow() so there is exactly one convention).
  - All money is integer cents. Never a float column, ever.
  - "state" columns are plain strings, validated by the *State enums below and
    by the transition functions in portal/state_machine.py -- not by a DB-level
    CHECK constraint, so a migration never needs to touch a constraint to add
    a state.
  - Anything a user or admin *chose* (a PriceSnapshot, a SubmissionVersion) is
    immutable once written: no update path is provided for those rows at all,
    only insert. That is what makes "what did they agree to" answerable later.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# State enums. Stored as their .value (a plain string) -- see the class
# docstring above for why this isn't a DB CHECK constraint.
# ---------------------------------------------------------------------------

class RoundState(str, enum.Enum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    IN_PRINT = "IN_PRINT"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class SubmissionState(str, enum.Enum):
    DRAFT = "DRAFT"
    CHECKING = "CHECKING"
    CHECK_FAILED = "CHECK_FAILED"
    CLEAN = "CLEAN"                 # GREEN, no fixes needed
    NEEDS_REVIEW = "NEEDS_REVIEW"    # YELLOW, auto-fixable
    NEEDS_FIXES = "NEEDS_FIXES"      # RED, blocks the whole order
    ACCEPTED = "ACCEPTED"
    LOCKED = "LOCKED"                # part of a locked order
    FROZEN = "FROZEN"                # part of a paid, frozen order
    IN_PRINT_RUN = "IN_PRINT_RUN"
    DONE = "DONE"
    WITHDRAWN = "WITHDRAWN"


class CheckRunState(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class OrderState(str, enum.Enum):
    OPEN = "OPEN"
    LOCKED = "LOCKED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    FROZEN = "FROZEN"
    IN_PRINT_RUN = "IN_PRINT_RUN"
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"


class PaymentKind(str, enum.Enum):
    PAYPAL = "PAYPAL"
    CASH = "CASH"
    ADJUSTMENT = "ADJUSTMENT"
    REFUND = "REFUND"


class JobState(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class PrintJobState(str, enum.Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ActorKind(str, enum.Enum):
    USER = "USER"
    ADMIN = "ADMIN"
    WORKER = "WORKER"
    SYSTEM = "SYSTEM"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Setting(Base):
    """Typed key/value store for everything an admin tunes at runtime.

    Known keys (seeded by portal/cli.py bootstrap):
      cost_per_card_cents (13), cost_per_list_cents (26),
      processing_charge_cents (0), paypal_me_handle, invite_code_hash,
      retention_days (90), intro_text_md
    """
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # SET NULL: same reasoning as AuditLog.actor_user_id -- an admin's later
    # self-deletion must not be blocked by having once edited a setting.
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Round(Base):
    __tablename__ = "rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)  # "2611"
    name: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default=RoundState.DRAFT.value)
    opens_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Rates snapshotted when the round opens -- see module docstring on why
    # this exists ABOVE AND BEYOND PriceSnapshot (which snapshots per-order).
    cost_per_card_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_per_list_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_charge_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_cards: Mapped[int | None] = mapped_column(Integer, nullable=True)

    submissions: Mapped[list["Submission"]] = relationship(back_populates="round")
    orders: Mapped[list["Order"]] = relationship(back_populates="round")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    person_slug: Mapped[str] = mapped_column(String(100), unique=True)  # ^[A-Za-z]+$
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)  # OPTIONAL
    notify: Mapped[bool] = mapped_column(Boolean, default=False)
    access_code_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    privacy_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    login_tokens: Mapped[list["LoginToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    submissions: Mapped[list["Submission"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    orders: Mapped[list["Order"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class LoginToken(Base):
    """A personal invite/login link. purpose=INVITE for the pre-issued Phase-1
    links; purpose=LOGIN reserved for a future magic-link flow (Phase 3)."""
    __tablename__ = "login_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    purpose: Mapped[str] = mapped_column(String(20), default="INVITE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="login_tokens")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="sessions")


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------

class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("round_id", "user_id", "ordinal", name="uq_submission_slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    ordinal: Mapped[int] = mapped_column(Integer)  # 1..n within (round, user)
    state: Mapped[str] = mapped_column(String(20), default=SubmissionState.DRAFT.value)
    # use_alter=True: these are the "pointer forward" half of a deliberate
    # cycle with SubmissionVersion.submission_id (a version always belongs to
    # a submission; a submission points at its current/accepted version).
    # Without it, DDL tooling (create_all/drop_all, Alembic autogenerate)
    # cannot linearly order the two tables' constraints.
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("submission_versions.id", use_alter=True, name="fk_submission_current_version"),
        nullable=True,
    )
    accepted_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("submission_versions.id", use_alter=True, name="fk_submission_accepted_version"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    round: Mapped["Round"] = relationship(back_populates="submissions")
    user: Mapped["User"] = relationship(back_populates="submissions")
    versions: Mapped[list["SubmissionVersion"]] = relationship(
        back_populates="submission",
        foreign_keys="SubmissionVersion.submission_id",
        cascade="all, delete-orphan",
    )
    current_version: Mapped["SubmissionVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )
    accepted_version: Mapped["SubmissionVersion | None"] = relationship(
        foreign_keys=[accepted_version_id], post_update=True
    )


class SubmissionVersion(Base):
    """Immutable. Every edit inserts a new row; nothing here is ever updated."""
    __tablename__ = "submission_versions"
    __table_args__ = (UniqueConstraint("submission_id", "version_no", name="uq_submission_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(String(10), default="PASTE")  # PASTE|TXT|PDF
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    submission: Mapped["Submission"] = relationship(
        back_populates="versions", foreign_keys=[submission_id]
    )
    check_run: Mapped["CheckRun | None"] = relationship(
        back_populates="submission_version", uselist=False, cascade="all, delete-orphan"
    )


class CheckRun(Base):
    __tablename__ = "check_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_version_id: Mapped[int] = mapped_column(
        ForeignKey("submission_versions.id", ondelete="CASCADE"), unique=True
    )
    state: Mapped[str] = mapped_column(String(20), default=CheckRunState.QUEUED.value)
    traffic_light: Mapped[str | None] = mapped_column(String(10), nullable=True)  # GREEN|YELLOW|RED
    scryfall_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    total_cards: Mapped[int] = mapped_column(Integer, default=0)
    unique_lines: Mapped[int] = mapped_column(Integer, default=0)
    audit_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    fixed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    fixed_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checker_version: Mapped[str | None] = mapped_column(String(40), nullable=True)

    submission_version: Mapped["SubmissionVersion"] = relationship(back_populates="check_run")


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("round_id", "user_id", "seq", name="uq_order_slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default=OrderState.OPEN.value)
    reference_code: Mapped[str] = mapped_column(String(40), unique=True)
    # use_alter=True: same deliberate cycle as Submission's version pointers
    # above, this time with PriceSnapshot.order_id.
    price_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("price_snapshots.id", use_alter=True, name="fk_order_price_snapshot"),
        nullable=True,
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    round: Mapped["Round"] = relationship(back_populates="orders")
    user: Mapped["User"] = relationship(back_populates="orders")
    price_snapshot: Mapped["PriceSnapshot | None"] = relationship(
        foreign_keys=[price_snapshot_id], post_update=True
    )
    submissions: Mapped[list["OrderSubmission"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    payments: Mapped[list["PaymentRecord"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    @property
    def total_paid_cents(self) -> int:
        return sum(p.amount_cents for p in self.payments)


class PriceSnapshot(Base):
    """Immutable: what the user agreed to. Never updated after creation."""
    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    cost_per_card_cents: Mapped[int] = mapped_column(Integer)
    cost_per_list_cents: Mapped[int] = mapped_column(Integer)
    processing_charge_cents: Mapped[int] = mapped_column(Integer)
    n_cards: Mapped[int] = mapped_column(Integer)
    n_lists: Mapped[int] = mapped_column(Integer)
    total_cents: Mapped[int] = mapped_column(Integer)
    breakdown_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrderSubmission(Base):
    """Which frozen submission *versions* an order covers."""
    __tablename__ = "order_submissions"

    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), primary_key=True
    )
    submission_version_id: Mapped[int] = mapped_column(ForeignKey("submission_versions.id"))
    n_cards: Mapped[int] = mapped_column(Integer)

    order: Mapped["Order"] = relationship(back_populates="submissions")


class PaymentRecord(Base):
    """Append-only. Never updated, never deleted (a mistake is corrected with
    an ADJUSTMENT or REFUND row, not by editing history)."""
    __tablename__ = "payment_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    amount_cents: Mapped[int] = mapped_column(Integer)  # negative for refunds
    kind: Mapped[str] = mapped_column(String(20), default=PaymentKind.PAYPAL.value)
    received_on: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nullable + SET NULL (same reasoning as AuditLog.actor_user_id): this
    # financial record belongs to the ORDER, not to the admin who happened to
    # record it, and must survive that admin's account being deleted later.
    recorded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    order: Mapped["Order"] = relationship(back_populates="payments")


# ---------------------------------------------------------------------------
# Jobs & audit
# ---------------------------------------------------------------------------

class Job(Base):
    """The in-process background check queue (portal/jobs.py)."""
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="CHECK")
    submission_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("submission_versions.id", ondelete="CASCADE"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(20), default=JobState.QUEUED.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PrintJob(Base):
    """Phase 2: the worker's job queue. Result never contains a file, only
    counts/sizes/names -- PDFs never touch the server."""
    __tablename__ = "print_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"))
    state: Mapped[str] = mapped_column(String(20), default=PrintJobState.PENDING.value)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    claimed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claim_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    progress_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Only one non-terminal print job per round.
        Index(
            "uq_print_job_active_per_round",
            "round_id",
            unique=True,
            sqlite_where=(state.in_(["PENDING", "CLAIMED", "RUNNING"])),  # type: ignore[has-type]
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # ondelete="SET NULL": the audit trail (accountability, GDPR Art. 5(2))
    # must outlive the actor. Without this, a self-deletion request would
    # fail with a FOREIGN KEY constraint error for any user who had ever done
    # anything logged -- which in practice is almost every real user.
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_kind: Mapped[str] = mapped_column(String(10), default=ActorKind.SYSTEM.value)
    action: Mapped[str] = mapped_column(String(100))
    subject_type: Mapped[str] = mapped_column(String(50))
    subject_id: Mapped[str] = mapped_column(String(50))
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ScryfallCacheRow(Base):
    """Backs portal/db_cache.py's SqliteScryfallCache. Survives VPS teardown
    inside the regular DB backup, so each round starts with a warm cache."""
    __tablename__ = "scryfall_cache"

    namespace: Mapped[str] = mapped_column(String(20), primary_key=True)
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    tokens: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
