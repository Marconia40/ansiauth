import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the backend/ package importable when running `alembic` from backend/.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Import all models so their tables are registered with Base.metadata before
# autogenerate compares them against the current DB state.
from app.db.base import Base        # noqa: E402
import app.db.models                # noqa: E402, F401

# Read DATABASE_URL from the environment (same source as the application).
from app.core.config import DATABASE_URL  # noqa: E402

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL)

# Configure Python logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without an active DB connection (emits SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite does not support many ALTER TABLE forms; batch mode rewrites the
        # whole table, which is the standard workaround.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
