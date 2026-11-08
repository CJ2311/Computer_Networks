"""Ordinary environment; NFC deployment uses alembic_nfc_demo.ini."""
import os
from logging.config import fileConfig
from pathlib import Path
from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import URL
from backend.alembic.safety import run_on_connection
from backend.app.base import Base
import backend.app.models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def database_url():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    return URL.create(
        drivername="postgresql+psycopg", username="inventory_migrator",
        password=os.environ["INVENTORY_MIGRATOR_PASSWORD"], host="localhost", port=5432,
        database=os.getenv("INVENTORY_DATABASE_NAME", "inventory_db"),
    )


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=Base.metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = config.attributes.get("connection")
    if connection is not None:
        run_on_connection(context, connection, Base.metadata)
    else:
        configuration = config.get_section(config.config_ini_section, {})
        configuration["sqlalchemy.url"] = database_url()
        engine = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                run_on_connection(context, connection, Base.metadata)
        finally:
            engine.dispose()
