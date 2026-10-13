from alembic import context
from sqlalchemy import create_engine

from legal_core.config import get_settings
from legal_core.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url") or get_settings().database_url
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")
run_migrations_online()
