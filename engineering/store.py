import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy import (
    event as sa_event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from engineering.schemas import ModelBudgetExceeded
from shortener.models import make_engine


def now():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "engineering_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    requirement: Mapped[str] = mapped_column(Text)
    scenario: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(String(10))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    action: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[str | None] = mapped_column(String(36))
    audit_seq: Mapped[int] = mapped_column(Integer, default=0)
    audit_head: Mapped[str] = mapped_column(String(64), default="0" * 64)


class Event(Base):
    __tablename__ = "engineering_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("engineering_runs.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    occurred_at: Mapped[str] = mapped_column(String(40))
    previous_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


def event_hash(run_id, revision, sequence, kind, payload, occurred_at, previous_hash):
    value = [run_id, revision, sequence, kind, payload, occurred_at, previous_hash]
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def append_event(db, run, kind, payload):
    run.audit_seq += 1
    occurred_at = now().isoformat()
    digest = event_hash(
        run.id, run.revision, run.audit_seq, kind, payload, occurred_at, run.audit_head
    )
    db.add(
        Event(
            run_id=run.id,
            revision=run.revision,
            sequence=run.audit_seq,
            kind=kind,
            payload=payload,
            occurred_at=occurred_at,
            previous_hash=run.audit_head,
            hash=digest,
        )
    )
    run.audit_head = digest
    run.updated_at = now()


class Store:
    def __init__(self, url):
        self.engine = make_engine(url)
        if self.engine.dialect.name == "sqlite":
            # SQLite ignores SELECT FOR UPDATE. Reserve the write lock before
            # reading the audit head or claiming work, including across processes.
            @sa_event.listens_for(self.engine, "connect")
            def disable_implicit_transactions(connection, _):
                connection.isolation_level = None

            @sa_event.listens_for(self.engine, "begin")
            def begin_immediate(connection):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        Base.metadata.create_all(self.engine)

    def event(self, run_id, kind, payload):
        with self.sessions.begin() as db:
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            append_event(db, run, kind, payload)

    def get(self, run_id):
        with self.sessions() as db:
            return db.get(Run, run_id)

    def model_calls_used(self, run_id, revision, checkpoint_count=0):
        with self.sessions() as db:
            latest = db.scalar(
                select(Event)
                .where(
                    Event.run_id == run_id,
                    Event.revision == revision,
                    Event.kind == "model.call_reserved",
                )
                .order_by(Event.sequence.desc())
                .limit(1)
            )
            return max(checkpoint_count, latest.payload["number"] if latest else 0)

    def reserve_model_call(self, run_id, revision, limit, purpose, checkpoint_count=0):
        # Reserve before the external call. Unlike a graph node's final return,
        # this survives a later task failure or a process crash mid-implementation.
        with self.sessions.begin() as db:
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run.revision != revision or run.status != "running":
                raise ModelBudgetExceeded("Run is no longer active at this revision")
            latest = db.scalar(
                select(Event)
                .where(
                    Event.run_id == run_id,
                    Event.revision == revision,
                    Event.kind == "model.call_reserved",
                )
                .order_by(Event.sequence.desc())
                .limit(1)
            )
            used = max(checkpoint_count, latest.payload["number"] if latest else 0)
            if used >= limit:
                raise ModelBudgetExceeded("Logical model call budget exhausted")
            append_event(
                db,
                run,
                "model.call_reserved",
                {"number": used + 1, "purpose": purpose, "limit": limit},
            )
            return used + 1

    def claim(self, owner, lease_seconds):
        with self.sessions.begin() as db:
            run = db.scalar(
                select(Run)
                .where(
                    (Run.status == "queued")
                    | ((Run.status == "running") & (Run.lease_until < now()))
                )
                .order_by(Run.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not run:
                return None
            previous = run.status
            run.status = "running"
            run.owner = owner
            run.lease_until = now() + timedelta(seconds=lease_seconds)
            append_event(
                db, run, "worker.claimed", {"owner": owner, "recovered": previous == "running"}
            )
            return run

    def heartbeat(self, run_id, owner, lease_seconds):
        with self.sessions.begin() as db:
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run.owner != owner or run.status == "cancelled":
                return False
            run.lease_until = now() + timedelta(seconds=lease_seconds)
            return True

    def finish(self, run_id, owner, state, status):
        with self.sessions.begin() as db:
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run.owner != owner or run.status == "cancelled":
                return
            run.state = state
            run.status = status
            run.action = None
            run.owner = None
            run.lease_until = None
            append_event(db, run, "run.status", {"status": status})

    def events(self, run_id):
        with self.sessions() as db:
            return list(
                db.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.sequence))
            )

    def audit_valid(self, run_id):
        # Read the head and its events together; active workers append under the
        # same row lock, so polling cannot report a false integrity failure.
        with self.sessions() as db:
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if not run:
                return False
            events = db.scalars(
                select(Event).where(Event.run_id == run_id).order_by(Event.sequence)
            )
            previous, sequence = "0" * 64, 0
            for event in events:
                sequence += 1
                if (
                    event.sequence != sequence
                    or event.previous_hash != previous
                    or event.hash
                    != event_hash(
                        event.run_id,
                        event.revision,
                        event.sequence,
                        event.kind,
                        event.payload,
                        event.occurred_at,
                        event.previous_hash,
                    )
                ):
                    return False
                previous = event.hash
            return run.audit_head == previous and run.audit_seq == sequence
