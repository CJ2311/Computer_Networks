import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import URL

from backend.app.database import Base
import backend.app.models  # noqa: F401


# Load environment variables
BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


# Alembic configuration
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# Use the dedicated migration account
DATABASE_URL = URL.create(
    drivername="postgresql+psycopg",
    username="inventory_migrator",
    password=os.environ["INVENTORY_MIGRATOR_PASSWORD"],
    host="localhost",
    port=5432,
    database="inventory_db",
)

# SQLAlchemy metadata for migration generation
target_metadata = Base.metadata


def run_migrations_offline():
    """Generate SQL migrations without connecting to the database."""

    context.configure(
        url=DATABASE_URL.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations using the migration database account."""

    configuration = config.get_section(config.config_ini_section, {})

    configuration["sqlalchemy.url"] = DATABASE_URL.render_as_string(
        hide_password=False
    )

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()