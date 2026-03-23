"""
Async PostgreSQL database connection and table management.
"""

import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from backend.config import settings
from backend.storage.models import Base

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
)

async_session_factory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db():
    """Create all tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized.")


async def get_session() -> AsyncSession:
    """Get an async database session."""
    async with async_session_factory() as session:
        return session


async def close_db():
    """Dispose the engine connection pool."""
    await engine.dispose()
    logger.info("Database connection pool closed.")
