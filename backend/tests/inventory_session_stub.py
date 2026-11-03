"""Transaction-aware mock wiring for existing database-free policy tests.

Real Session ownership/event behavior is tested separately. Never used by app code.
"""
from unittest.mock import Mock
from sqlalchemy.engine import Engine
from backend.app.services import inventory_transactions as transactions


def configure_session(db, monkeypatch):
    db.is_active = True
    db.in_transaction.return_value = False
    db.get_transaction.return_value = None
    engine = Mock(spec=Engine)
    db.get_bind.return_value = engine
    root = Mock(is_active=True)
    def begin():
        root.is_active = True
        db.in_transaction.return_value = True
        db.get_transaction.return_value = root
        return root
    def commit():
        db.commit()
        root.is_active = False
        db.in_transaction.return_value = False
        db.get_transaction.return_value = None
    def rollback():
        db.rollback()
        root.is_active = False
        db.in_transaction.return_value = False
        db.get_transaction.return_value = None
    db.begin.side_effect = begin
    root.commit.side_effect, root.rollback.side_effect = commit, rollback
    install = transactions._install_guards
    def install_for_real_sessions(session, owner):
        if not isinstance(session, Mock):
            return install(session, owner)
    monkeypatch.setattr(transactions, "_install_guards", install_for_real_sessions)
    return root
