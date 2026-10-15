"""Synchronous SQLAlchemy infrastructure; migrations, not startup, create schema."""
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from sourcedesk.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine():
    return create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=5,
                         max_overflow=5, connect_args={"connect_timeout": 5})


@lru_cache
def session_factory():
    return sessionmaker(bind=get_engine(), class_=Session, expire_on_commit=False)


def get_session():
    with session_factory()() as session:
        try:
            yield session
        finally:
            session.rollback()
