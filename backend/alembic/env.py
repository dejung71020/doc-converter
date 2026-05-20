import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool
from alembic import context

from app.core.config import settings
from app.models.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    오프라인 모드에서 마이그레이션 실행.
    DB에 직접 연결하지 않고 SQL 스크립트만 생성할 때 사용.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """
    실제 DB 커넥션을 받아 마이그레이션을 실행.
    run_async_migrations()에서 비동기 커넥션을 동기 컨텍스트로 넘겨줄 때 호출됨.
    """
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    비동기 엔진으로 DB에 연결해 마이그레이션 실행.
    asyncpg 드라이버를 사용하기 때문에 async 방식으로 처리해야 함.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """
    온라인 모드에서 마이그레이션 실행 (일반적인 사용 방식).
    비동기 마이그레이션 함수를 asyncio.run()으로 감싸서 실행.
    """
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()