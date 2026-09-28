import os
from sqlalchemy import String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gateway.db")
IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # MySQL/Postgres/etc: pre_ping + recycle so a long-idle pooled connection
    # doesn't surface as "server has gone away"; check_same_thread is a
    # SQLite-only connect arg and must not be passed here.
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # Sessions commit from both the event loop and executor threads. WAL
        # lets readers coexist with the writer; busy_timeout makes brief
        # write collisions wait instead of raising "database is locked".
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from db import models  # noqa: F401 — registers all ORM classes with Base

    if not IS_SQLITE:
        _apply_varchar_lengths()
    Base.metadata.create_all(bind=engine)
    # create_all only creates missing tables — it never alters existing ones —
    # so columns added after a DB (SQLite or MySQL) was first created need this
    # additive pass too.
    _apply_light_migrations()
    db = SessionLocal()
    try:
        _seed(db)
    finally:
        db.close()


def _apply_varchar_lengths(default: int = 255) -> None:
    """MySQL (unlike SQLite) requires a length on every VARCHAR. The models
    declare bare `String`, so give every unbounded String column a default
    length before create_all. `type(col.type) is String` is an exact-type
    check that deliberately excludes Text (a String subclass) so large text
    fields stay TEXT rather than being capped to VARCHAR(default)."""
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if type(col.type) is String and col.type.length is None:
                col.type.length = default


def _apply_light_migrations() -> None:
    """Add columns that were introduced after the initial schema shipped.

    `create_all` only creates missing tables — it never alters existing ones —
    so ADD COLUMN has to be issued manually. Guarded per-column: swallow the
    "duplicate column" error SQLite raises when the column already exists.
    """
    from sqlalchemy import text

    additions = [
        ("order_lots", "target_enabled",      "BOOLEAN NOT NULL DEFAULT 0"),
        ("order_lots", "target_value",        "FLOAT   NOT NULL DEFAULT 0.0"),
        ("order_lots", "persistent_order_id", "INTEGER"),
        ("order_lots", "is_external",         "BOOLEAN NOT NULL DEFAULT 0"),
        ("order_lots", "description",         "TEXT    NOT NULL DEFAULT ''"),
        ("persistent_orders", "description",  "TEXT    NOT NULL DEFAULT ''"),
        ("order_lots", "carried_pnl",         "FLOAT   NOT NULL DEFAULT 0.0"),
        ("rollover_intents", "acknowledged",  "BOOLEAN NOT NULL DEFAULT 0"),
        ("rollover_intents", "carry_pnl",     "BOOLEAN NOT NULL DEFAULT 1"),
        ("symbol_targets", "carried_pnl",     "FLOAT   NOT NULL DEFAULT 0.0"),
        ("order_lots", "is_rollover",         "BOOLEAN NOT NULL DEFAULT 0"),
        ("accounts",   "user_id",             "INTEGER"),
        ("order_lots", "is_reentry",          "BOOLEAN NOT NULL DEFAULT 0"),
        ("order_lots", "is_temp_exit",        "BOOLEAN NOT NULL DEFAULT 0"),
        ("order_lots", "reentry_source_lot_id", "INTEGER"),
        ("order_lots", "source_service",      "VARCHAR(255)"),
    ]
    with engine.begin() as conn:
        for table, col, coltype in additions:
            try:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {coltype}'))
            except Exception:
                pass  # already added
        try:
            # per_lot_value → target_value: symbol target switched from a
            # per-lot-scaled threshold to a flat total-P&L threshold.
            conn.execute(text('ALTER TABLE symbol_targets RENAME COLUMN per_lot_value TO target_value'))
        except Exception:
            pass  # already renamed (or fresh table already has target_value)


def _seed(db) -> None:
    from db.models import Broker

    _ensure(db, Broker, name="shoonya")
    _ensure(db, Broker, name="sharekhan")

    db.commit()


def _ensure(db, model, defaults: dict | None = None, **lookup):
    """Insert a row if it doesn't already exist. Matches on lookup kwargs."""
    obj = db.query(model).filter_by(**lookup).first()
    if obj is None:
        kwargs = {**lookup, **(defaults or {})}
        db.add(model(**kwargs))
