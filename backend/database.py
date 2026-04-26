"""
database.py — SQLAlchemy engine + session factory.

Supabase / PostgreSQL in production, SQLite fallback for local dev.

Environment variables:
  DATABASE_URL  — set in Render to your Supabase connection string.
                  Leave unset locally to use SQLite (./app.db).

Supabase connection string format (Transaction Pooler — port 5432):
  postgresql://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:5432/postgres?sslmode=require

Find it in:
  Supabase Dashboard → Project Settings → Database → Connection string → URI
  Then switch to "Transaction pooler" and copy that string.

⚠ Always append ?sslmode=require — Supabase rejects unencrypted connections.
"""

import os
import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

if not DATABASE_URL:
    # ── Local dev: SQLite ────────────────────────────────────────────────────
    DATABASE_URL = "sqlite:///./app.db"
    log.info("💾 Database: SQLite (local dev) — set DATABASE_URL for Supabase")
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=False,
    )

else:
    # ── Production: PostgreSQL / Supabase ────────────────────────────────────
    # Supabase sometimes returns "postgres://" — SQLAlchemy requires "postgresql://"
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    # Supabase Supavisor / pgbouncer (transaction pooler) drops idle
    # connections after ~5 min. pool_pre_ping and pool_recycle handle this.
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,    # test connection health before use
        pool_size=5,           # maintain 5 persistent connections
        max_overflow=10,       # allow 10 extra under spike load
        pool_recycle=240,      # recycle before Supabase's 300 s idle timeout
        echo=False,
    )
    log.info(
        "🐘 Database: PostgreSQL / Supabase  url_prefix=%s…",
        DATABASE_URL[:40],
    )


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a DB session, always closes on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
