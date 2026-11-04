"""Explicit ownership and connection-local flush guards for inventory writes."""
from dataclasses import dataclass, field
from functools import wraps

from fastapi import HTTPException
from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session


_OWNER_KEY = "inventory_transaction_owner"


def _conflict(message):
    return HTTPException(status_code=409, detail=message)


def clean_session(db: Session) -> None:
    """Pending-state check, also used by read services; not proof of ownership."""
    if db.new or db.dirty or db.deleted or db.in_nested_transaction() or not db.is_active:
        raise _conflict("Inventory requires a clean, active, non-nested session")


@dataclass(eq=False)
class _Owner:
    root: object
    engine: Engine
    expected: object = None
    phase: str = "running"
    listeners: list = field(default_factory=list)


def require_owner(db: Session) -> _Owner:
    owner = db.info.get(_OWNER_KEY)
    if (not isinstance(owner, _Owner) or owner.root is None or db.get_transaction() is not owner.root
            or not owner.root.is_active or not db.is_active or db.in_nested_transaction()):
        raise _conflict("Inventory transaction ownership changed or ended unexpectedly")
    return owner


def _pending_stage(db, expected):
    if db.deleted or any(obj is not expected for obj in (*db.new, *db.dirty)):
        raise _conflict("Unexpected pending ORM changes during inventory operation")
    state = inspect(expected)
    # These workflows change scalar fields only. Do not let UOW relationship
    # synchronization/cascades add hidden writes during a guarded scalar stage.
    if any(state.attrs[rel.key].history.has_changes() for rel in state.mapper.relationships):
        raise _conflict("Relationship changes are not allowed in an inventory flush stage")


def _install_guards(db: Session, owner: _Owner):
    def listen(target, name, callback, **kwargs):
        event.listen(target, name, callback, **kwargs)
        owner.listeners.append((target, name, callback))

    def capture_root(session, root):
        if root.parent is None and owner.root is None:
            owner.root = root

    def before_flush(session, context, instances):
        current = require_owner(session)
        if current.expected is None or current.phase != "running":
            raise _conflict("Inventory flush outside an approved persistence stage")
        _pending_stage(session, current.expected)

    def before_commit(session):
        current = require_owner(session)
        if current.phase != "committing" or current.expected is not None:
            raise _conflict("Premature inventory transaction completion")
        clean_session(session)

    def orm_execute(state):
        require_owner(db)
        if db.get_bind(**state.bind_arguments) is not owner.engine:
            raise _conflict("Inventory requires one Engine-bound session")

    def after_begin(session, root, connection):
        # During this event SQLAlchemy is still provisioning the connection;
        # is_active is temporarily false. Check identity here, then enforce
        # active-state checks at query/flush/SQL/commit boundaries.
        if (session.info.get(_OWNER_KEY) is not owner or session.get_transaction() is not owner.root
                or root is not owner.root or connection.engine is not owner.engine):
            raise _conflict("Unsupported inventory connection ownership")

        def before_sql(conn, cursor, statement, parameters, context, executemany):
            require_owner(db)
            if not (context.isinsert or context.isupdate or context.isdelete):
                return
            expected = owner.expected
            if expected is None or owner.phase != "running" or context.isdelete:
                raise _conflict("Inventory write outside an approved persistence stage")
            _pending_stage(db, expected)
            state = inspect(expected)
            compiled = context.compiled
            if compiled is None or compiled.statement.table is not state.mapper.local_table:
                raise _conflict("Unexpected table in inventory flush stage")
            rows = context.compiled_parameters
            if len(rows) != 1:
                raise _conflict("Unexpected batch in inventory flush stage")
            # All current inventory tables have a single UUID id. UOW updates
            # bind the primary key as <table>_id; inserts bind it as id.
            pk, = state.mapper.local_table.primary_key.columns
            key = pk.name if context.isinsert else f"{pk.table.name}_{pk.name}"
            identifier = state.dict.get(pk.key)
            if key not in rows[0] or (identifier is not None and rows[0][key] != identifier):
                raise _conflict("Unexpected record in inventory flush stage")
            if context.isinsert and not state.pending:
                raise _conflict("Unexpected insert in inventory flush stage")
            if context.isupdate and not state.persistent:
                raise _conflict("Unexpected update in inventory flush stage")

        # Connection-local listener: no global Engine/Mapper behavior changes,
        # and concurrent inventory operations do not modify shared dispatchers.
        listen(connection, "before_cursor_execute", before_sql)

    # Capture identity before other creation callbacks can fail or interrupt.
    listen(db, "after_transaction_create", capture_root, insert=True)
    listen(db, "before_flush", before_flush)
    listen(db, "before_commit", before_commit)
    listen(db, "do_orm_execute", orm_execute)
    listen(db, "after_begin", after_begin)


def _remove_guards(owner):
    for target, name, callback in reversed(owner.listeners):
        event.remove(target, name, callback)
    owner.listeners.clear()


def flush_stage(db: Session, expected) -> None:
    _pending_stage(db, expected)
    owner = require_owner(db)
    if owner.phase != "running" or owner.expected is not None:
        raise _conflict("Recursive inventory flush stage")
    if db.get_bind(mapper=inspect(expected).mapper) is not owner.engine:
        raise _conflict("Inventory requires one Engine-bound session")
    owner.expected = expected
    try:
        db.flush()
        require_owner(db)
        clean_session(db)
    finally:
        owner.expected = None


def _rollback_owned(db, owner):
    # Never Session.rollback(): it could end a replacement/caller transaction.
    if owner.root is not None and db.get_transaction() is owner.root:
        owner.phase = "rolling_back"
        owner.root.rollback()


def transaction(operation=None, *, uncertain_commit_detail: str | None = None):
    if operation is None:
        return lambda target: transaction(target, uncertain_commit_detail=uncertain_commit_detail)
    @wraps(operation)
    def run(db: Session, *args, **kwargs):
        clean_session(db)
        if db.in_transaction() or db.info.get(_OWNER_KEY) is not None:
            raise _conflict("Inventory writes require a fresh session without an existing transaction")
        try:
            engine = db.get_bind()
        except Exception:
            raise _conflict("Inventory requires an Engine-bound session") from None
        if not isinstance(engine, Engine):
            raise _conflict("Inventory requires an Engine-bound session")
        owner = _Owner(None, engine)
        db.info[_OWNER_KEY] = owner
        try:
            _install_guards(db, owner)
            root = db.begin()
            if owner.root is not None and owner.root is not root:
                raise _conflict("Inventory transaction ownership changed during begin")
            owner.root = root
            with db.no_autoflush:
                result = operation(db, *args, **kwargs)
                require_owner(db)
                clean_session(db)
                owner.phase = "committing"
                owner.root.commit()
                return result
        except BaseException as error:
            commit_attempted = owner.phase == "committing"
            try:
                _rollback_owned(db, owner)
            except Exception:
                detail = (uncertain_commit_detail if commit_attempted and uncertain_commit_detail
                          else "Inventory transaction cleanup failed")
                raise HTTPException(status_code=503, detail=detail) from None
            if isinstance(error, IntegrityError):
                raise _conflict("Inventory operation conflicts with current database state") from None
            if isinstance(error, DBAPIError):
                if getattr(error.orig, "sqlstate", None) in {"40001", "40P01", "55P03"}:
                    raise _conflict("Concurrent inventory operation; reload and retry") from None
                detail = (uncertain_commit_detail if commit_attempted and uncertain_commit_detail
                          else "Inventory database operation failed")
                raise HTTPException(status_code=503, detail=detail) from None
            raise
        finally:
            try:
                _remove_guards(owner)
            finally:
                if db.info.get(_OWNER_KEY) is owner:
                    db.info.pop(_OWNER_KEY)
    return run
