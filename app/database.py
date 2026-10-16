import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

db_url = settings.database_url
# Fix Render/Heroku postgres:// URLs to postgresql:// for SQLAlchemy 2.0
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

connect_args = {}
if db_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    db_url,
    connect_args=connect_args,
    pool_pre_ping=True
)

# Enable WAL mode and foreign keys for SQLite
if db_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        from sqlalchemy import text
        for col_stmt in [
            "ALTER TABLE decks ADD COLUMN include_tokens BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE decks ADD COLUMN include_stickers BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE decks ADD COLUMN replace_date_stamps BOOLEAN DEFAULT 1 NOT NULL",
            "ALTER TABLE decks ADD COLUMN replace_digital BOOLEAN DEFAULT 1 NOT NULL",
        ]:
            try:
                conn.execute(text(col_stmt))
                conn.commit()
            except Exception:
                pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

