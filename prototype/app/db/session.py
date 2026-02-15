from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.config import settings

# 1. The Engine (The Power Plant)
# This manages the pool of connections to Postgres.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=True,  # Shows SQL queries in the terminal (great for debugging)
    future=True
)

# 2. The Session Maker (The Factory)
# When a user hits an API endpoint, this factory hands out a "session"
# (a temporary handle) to talk to the database.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)