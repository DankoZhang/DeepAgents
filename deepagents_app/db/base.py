"""SQLAlchemy Declarative Base。"""

from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase


class Base(AsyncAttrs, DeclarativeBase):
    """所有 ORM 模型的基类（含 AsyncAttrs，支持 awaitable_attrs）。"""
