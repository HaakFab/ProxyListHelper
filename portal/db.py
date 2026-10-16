"""SQLAlchemy engine/session setup, with the SQLite pragmas the design relies on.

WAL mode lets the background job thread write while a request reads; a longer
busy_timeout absorbs the brief lock contention that causes instead of surfacing
as an error; foreign_keys=ON makes the cascade/restrict rules in models.py real
(SQLite ignores them by default).
"""
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    connect_args = {}
    engine_kwargs = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if ":memory:" in database_url:
            # A plain sqlite:///:memory: engine hands out a FRESH, empty
            # database to every new connection the pool opens -- the second
            # session in a test would silently see none of the first
            # session's rows (and get "no such table" or FK errors instead of
            # a data problem). StaticPool pins the whole engine to one
            # single underlying connection, so all sessions share one
            # in-memory DB for the engine's lifetime. File-based URLs (real
            # deployments) don't need this: every connection opens the same
            # file.
            engine_kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, connect_args=connect_args, future=True, **engine_kwargs)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def init_engine(database_url: str) -> Engine:
    """Idempotent: safe to call once at startup and again in tests."""
    global _engine, _SessionLocal
    _engine = make_engine(database_url)
    # autoflush=True (SQLAlchemy's own default, kept explicit here): a query
    # issued after adding a row in the same session must see that row without
    # every caller having to remember an explicit .flush() first. Disabling
    # this once caused a real bug -- state_machine.edit()'s lookup of a
    # submission's order silently missed a just-added OrderSubmission link.
    _SessionLocal = sessionmaker(bind=_engine, autoflush=True, autocommit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database engine not initialised -- call init_engine() at startup")
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """For use outside a request: the job thread, the CLI, tests."""
    if _SessionLocal is None:
        raise RuntimeError("Database engine not initialised -- call init_engine() at startup")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Commits on success, rolls back on exception."""
    if _SessionLocal is None:
        raise RuntimeError("Database engine not initialised -- call init_engine() at startup")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
