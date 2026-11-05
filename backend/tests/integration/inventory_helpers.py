"""Pre-operation test setup that never starts the service's write transaction."""
from sqlalchemy import event, func, select, text


def database_now(database):
    with database.connect() as connection:
        return connection.scalar(select(func.clock_timestamp()))


def label_transaction(db, label):
    def label_owned_transaction(session, root, connection):
        connection.execute(text("SELECT set_config('application_name', :label, true)"), {"label": label})
    event.listen(db, "after_begin", label_owned_transaction)


def set_metadata_actor(executor, actor_id):
    """Set the audit actor in the test's current transaction only."""
    executor.execute(select(func.set_config("inventory.metadata_actor_id", str(actor_id), True)))
