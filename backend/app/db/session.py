"""Synchronous SQLAlchemy engine/session factory (psycopg 3)."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def create_db_engine() -> Engine:
    return create_engine(settings.postgres_url, pool_pre_ping=True)


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
