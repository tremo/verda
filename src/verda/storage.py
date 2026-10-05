from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, create_engine, event, inspect, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(primary_key=True)


class Snapshot(Base):
    __tablename__ = "legacy_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    imported_at: Mapped[str] = mapped_column(String(40))
    source_schema: Mapped[dict] = mapped_column(JSON)
    counts: Mapped[dict] = mapped_column(JSON)
    warnings: Mapped[dict] = mapped_column(JSON)
    mode: Mapped[str] = mapped_column(String(20), default="shadow")


class SourceRow(Base):
    __tablename__ = "legacy_rows"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("legacy_snapshots.id"), primary_key=True)
    table_name: Mapped[str] = mapped_column(String(40), primary_key=True)
    row_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str | None] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (Index("legacy_subject", "snapshot_id", "subject_id", "table_name"),)


class Case(Base):
    __tablename__ = "cases"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("legacy_snapshots.id"), primary_key=True)
    listing_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    lifecycle: Mapped[str] = mapped_column(String(50))
    source_revision: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String)
    parcel_key: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String(40))
    next_action: Mapped[str] = mapped_column(String(50), default="migration_review")


class TimelineEvent(Base):
    __tablename__ = "case_events"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("legacy_snapshots.id"), primary_key=True)
    source_event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[str | None] = mapped_column(String(100))
    at: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(200))
    source_task_id: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict | list | str | None] = mapped_column(JSON)
    __table_args__ = (Index("case_timeline", "snapshot_id", "listing_id", "source_event_id"),)


class DashboardSnapshot(Base):
    __tablename__ = "dashboard_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    imported_at: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)


def connect(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if parsed.database and parsed.database != ":memory:":
            path = Path(parsed.database).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.touch(mode=0o600)
            parsed = parsed.set(database=str(path))
        engine = create_engine(parsed, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
        return engine
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("Only SQLite development storage and PostgreSQL are supported")
    return create_engine(parsed)


def initialize(engine: Engine) -> None:
    """Explicit bootstrap; importing the module or serving never migrates a DB."""
    tables = set(inspect(engine).get_table_names())
    if tables:
        if SchemaVersion.__tablename__ not in tables:
            raise ValueError("Refusing to initialize a nonempty, unversioned database")
        with Session(engine) as session:
            if list(session.scalars(select(SchemaVersion.version))) != [1]:
                raise ValueError("Unsupported database schema version")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        versions = list(session.scalars(select(SchemaVersion.version)))
        if versions and versions != [1]:
            raise ValueError("Unsupported database schema version")
        if not versions:
            session.add(SchemaVersion(version=1))


@contextmanager
def transaction(engine: Engine):
    with Session(engine) as session, session.begin():
        yield session
