#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   base.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   base.py

SQLAlchemy Declarative Base。
"""

from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase


class Base(AsyncAttrs, DeclarativeBase):
    """所有 ORM 模型的基类（含 AsyncAttrs，支持 awaitable_attrs）。"""
