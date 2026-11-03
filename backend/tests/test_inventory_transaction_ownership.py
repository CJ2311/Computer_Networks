"""Real SQLAlchemy Sessions over a recording DBAPI stub: no database exists."""
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException
import pytest
from sqlalchemy import ForeignKey, Integer, event, insert, literal, select
from sqlalchemy.engine import Engine, URL
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import StaticPool

from backend.app.services.inventory_transactions import transaction, flush_stage


class Base(DeclarativeBase):
    pass


class Parent(Base):
    __tablename__ = "ownership_parent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
    children: Mapped[list["Child"]] = relationship(back_populates="parent")


class Child(Base):
    __tablename__ = "ownership_child"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey(Parent.id))
    parent: Mapped[Parent] = relationship(back_populates="children")


class StubDialect(DefaultDialect):
    name = "recording_stub"
    driver = "no_database"
    supports_statement_cache = True


@pytest.fixture
def recording_engine():
    state = SimpleNamespace(sql=[], commits=0, rollbacks=0)
    class Cursor:
        rowcount = 1
        lastrowid = None
        description = None
        def execute(self, statement, parameters):
            state.sql.append(statement)
            if statement.lstrip().upper().startswith("SELECT"):
                self.description = [("value", None, None, None, None, None, None)]
        def fetchone(self):
            return (1,)
        def close(self):
            pass
    class Connection:
        def cursor(self):
            return Cursor()
        def commit(self):
            state.commits += 1
        def rollback(self):
            state.rollbacks += 1
        def close(self):
            pass
    class StubError(Exception):
        pass
    dbapi = SimpleNamespace(paramstyle="named", Error=StubError)
    engine = Engine(StaticPool(creator=Connection), StubDialect(dbapi=dbapi), URL.create("recording_stub"))
    try:
        yield engine, state
    finally:
        engine.dispose()


@transaction
def noop(db):
    return "ok"


def rejected(call):
    with pytest.raises(HTTPException) as error:
        call()
    assert error.value.status_code == 409


@pytest.mark.parametrize("kind", ["explicit", "flushed", "sql", "savepoint", "pending"])
def test_caller_transactions_are_not_committed_or_rolled_back(recording_engine, kind):
    engine, state = recording_engine
    with Session(engine) as db:
        if kind == "explicit":
            db.begin()
        elif kind == "savepoint":
            db.begin_nested()
        elif kind in {"flushed", "pending"}:
            db.add(Parent(id=1))
            if kind == "flushed":
                db.flush()
        else:
            db.execute(insert(Parent).values(id=1))
        root, nested = db.get_transaction(), db.get_nested_transaction()
        before = (list(state.sql), state.commits, state.rollbacks, set(db.new))
        rejected(lambda: noop(db))
        assert db.get_transaction() is root and root.is_active
        assert db.get_nested_transaction() is nested
        assert before == (state.sql, state.commits, state.rollbacks, set(db.new))
        assert not db.info


def test_external_connection_transaction_remains_untouched(recording_engine):
    engine, state = recording_engine
    with engine.connect() as connection, connection.begin() as external:
        with Session(connection) as db:
            before = state.commits, state.rollbacks
            rejected(lambda: noop(db))
            assert external.is_active and not db.in_transaction()
            assert before == (state.commits, state.rollbacks)


def test_failed_session_is_rejected_without_rollback(recording_engine):
    engine, state = recording_engine
    with Session(engine) as db:
        db.add(Parent(id=1))
        def fail(*args):
            raise RuntimeError("flush failure")
        event.listen(db, "before_flush", fail)
        # A DBAPI/SQL flush failure puts the root into failed state. Use a
        # connection event so failure occurs inside SQLAlchemy's UOW boundary.
        event.remove(db, "before_flush", fail)
        event.listen(engine, "before_cursor_execute", fail)
        with pytest.raises(RuntimeError):
            db.flush()
        assert not db.is_active
        root, before = db.get_transaction(), state.rollbacks
        rejected(lambda: noop(db))
        assert db.get_transaction() is root and state.rollbacks == before
        event.remove(engine, "before_cursor_execute", fail)


def test_fresh_session_owns_root_and_cleans_flag(recording_engine):
    engine, _ = recording_engine
    with Session(engine) as db:
        assert noop(db) == "ok"
        assert not db.in_transaction() and not db.info


@pytest.mark.parametrize("interruption", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_exception_or_interruption_rolls_back_only_owned_root(recording_engine, interruption):
    engine, state = recording_engine
    @transaction
    def operation(db):
        obj = Parent(id=1)
        db.add(obj)
        flush_stage(db, obj)
        raise interruption("interrupted")
    with Session(engine) as db:
        with pytest.raises(interruption):
            operation(db)
        assert not db.in_transaction() and not db.info and state.commits == 0
        assert state.rollbacks > 0
        # Scoped listeners must be removed: caller can use the Session normally.
        db.add(Parent(id=2))
        db.flush()


def test_recursive_operation_cannot_end_outer_early(recording_engine):
    engine, state = recording_engine
    @transaction
    def operation(db):
        noop(db)
    with Session(engine) as db:
        rejected(lambda: operation(db))
        assert not db.in_transaction() and not db.info and state.commits == 0


def test_replacement_transaction_is_not_committed_or_rolled_back(recording_engine):
    engine, _ = recording_engine
    replacement = []
    @transaction
    def operation(db):
        db.get_transaction().rollback()
        replacement.append(db.begin())
    with Session(engine) as db:
        rejected(lambda: operation(db))
        assert db.get_transaction() is replacement[0] and replacement[0].is_active
        assert not db.info


def test_premature_commit_is_blocked(recording_engine):
    engine, state = recording_engine
    @transaction
    def operation(db):
        db.commit()
    with Session(engine) as db:
        rejected(lambda: operation(db))
        assert not db.in_transaction() and not db.info and state.commits == 0


@pytest.mark.parametrize("register_late", [False, True])
def test_before_commit_cannot_insert_unrelated_object(recording_engine, register_late):
    engine, state = recording_engine
    def inject(db):
        db.add(Parent(id=99))
    @transaction
    def operation(db):
        if register_late:
            event.listen(db, "before_commit", inject)
    with Session(engine) as db:
        if not register_late:
            event.listen(db, "before_commit", inject)
        rejected(lambda: operation(db))
        assert not state.sql and state.commits == 0 and not db.info


@pytest.mark.parametrize("related", [False, True])
def test_late_before_flush_cascade_is_blocked_before_sql(recording_engine, related):
    engine, state = recording_engine
    @transaction
    def operation(db):
        expected = Parent(id=1)
        db.add(expected)
        def inject(session, context, instances):
            if related:
                expected.children.append(Child(id=2))
            else:
                session.add(Parent(id=99))
        event.listen(db, "before_flush", inject)  # Runs AFTER our stage guard.
        flush_stage(db, expected)
    with Session(engine) as db:
        rejected(lambda: operation(db))
        assert not state.sql and state.commits == 0 and not db.info


def test_query_autoflush_is_suppressed_but_approved_flush_still_works(recording_engine):
    engine, state = recording_engine
    @transaction
    def operation(db):
        obj = Parent(id=1)
        db.add(obj)
        assert db.scalar(select(literal(1))) == 1
        assert not any(sql.startswith("INSERT") for sql in state.sql)
        flush_stage(db, obj)
    with Session(engine, autoflush=True) as db:
        operation(db)
        assert sum(sql.startswith("INSERT") for sql in state.sql) == 1
        assert state.commits == 1 and not db.info


def test_unapproved_explicit_flush_is_rejected(recording_engine):
    engine, state = recording_engine
    @transaction
    def operation(db):
        db.add(Parent(id=1))
        db.flush()
    with Session(engine) as db:
        rejected(lambda: operation(db))
        assert not state.sql and state.commits == 0


def test_approved_update_uses_owned_record_only(recording_engine):
    engine, state = recording_engine
    @transaction
    def create(db):
        obj = Parent(id=1)
        db.add(obj)
        flush_stage(db, obj)
        return obj
    @transaction
    def update(db, obj):
        obj.value = 2
        flush_stage(db, obj)
    with Session(engine, expire_on_commit=False) as db:
        obj = create(db)
        update(db, obj)
        assert obj.value == 2
        assert sum(sql.startswith("UPDATE ownership_parent") for sql in state.sql) == 1
        assert state.commits == 2


def test_authentication_and_write_dependencies_supply_distinct_sessions(recording_engine, monkeypatch):
    from backend.app import database
    engine, _ = recording_engine
    monkeypatch.setattr(database, "SessionLocal", lambda: Session(engine))
    authentication = database.get_db()
    write = database.get_inventory_write_db()
    auth_session, write_session = next(authentication), next(write)
    try:
        assert auth_session is not write_session
        assert auth_session.scalar(select(literal(1))) == 1
        root = auth_session.get_transaction()
        assert not write_session.in_transaction()
        assert noop(write_session) == "ok"
        assert auth_session.get_transaction() is root and root.is_active
        assert not write_session.in_transaction()
    finally:
        write.close()
        authentication.close()


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_begin_callback_failure_cleans_up_captured_owned_root(recording_engine, error_type):
    engine, state = recording_engine
    with Session(engine) as db:
        def fail(session, root):
            raise error_type("Interrupted during transaction creation")
        event.listen(db, "after_transaction_create", fail)
        with pytest.raises(error_type):
            noop(db)
        assert not db.in_transaction() and not db.info
        assert not state.sql and state.commits == 0
