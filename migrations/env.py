"""Migrations read environment settings; credentials never live in alembic.ini."""
from alembic import context
from sqlalchemy import create_engine, pool

from sourcedesk import models  # noqa: F401
from sourcedesk.config import get_settings
from sourcedesk.db import Base

config = context.config
settings = get_settings()
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(url=settings.database_url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
