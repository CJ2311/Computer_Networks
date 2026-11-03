"""Pre-operation test setup that never starts the service's write transaction."""
from sqlalchemy import event, func, select, text


def database_now(database):
    with database.connect() as connection:
        return connection.scalar(select(func.clock_timestamp()))


def label_transaction(db, label):
    def label_owned_transaction(session, root, connection):
        connection.execute(text("SELECT set_config('application_name', :label, true)"), {"label": label})
    event.listen(db, "after_begin", label_owned_transaction)
