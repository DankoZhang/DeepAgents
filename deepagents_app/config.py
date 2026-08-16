#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   config.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   config.py

应用配置
========

集中管理模型、路径、功能开关等运行时参数。

设计要点：
- 使用 ``pydantic-settings`` 从环境变量 / ``.env`` 自动加载
- 路径统一解析为绝对路径，避免 cwd 变化导致 workspace 漂移
- 业务代码只依赖 ``get_settings()``，不直接读 ``os.environ``
- Settings 为 frozen：禁止就地改字段；临时覆盖用 ``settings_with`` / ``model_copy``
- ``get_settings()`` 只读配置；目录创建由 lifespan / bootstrap 显式调用 ``ensure_directories()``
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from deepagents_app.constants import ModelProvider

# 项目根目录（本仓库 DeepAgents/）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """DeepAgents 演示框架的全局配置（不可变单例字段）。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ── 模型 ──────────────────────────────────────────────────────────
    model_provider: ModelProvider = "openai"
    model_name: str = "gpt-4o"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    anthropic_api_key: str | None = None

    # ── 路径 ──────────────────────────────────────────────────────────
    # Agent 可读写的工作区（FilesystemBackend 根目录）
    workspace_dir: Path = Field(default=PROJECT_ROOT / "workspace")
    # Memory 文件（AGENTS.md）——启动时注入主 Agent
    memory_file: Path = Field(default=PROJECT_ROOT / "AGENTS.md")
    # Redis checkpointer 连接串（多轮对话持久化）
    # 需要 Redis 8+ 或 Redis Stack（含 RedisJSON + RediSearch）
    redis_url: str = "redis://localhost:6379"

    # PostgreSQL：方法论 / Agent / Tool / Middleware / Conversation 配置库
    database_url: str = (
        "postgresql+psycopg://deepagents:deepagents@localhost:5432/deepagents"
    )

    # FastAPI
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    # 进程数：1=单进程；>1 时 uvicorn --workers / gunicorn UvicornWorker
    api_workers: int = Field(default=1, ge=1, le=64)
    # 启动器：uvicorn（内置多 worker）或 gunicorn+UvicornWorker
    api_server: Literal["uvicorn", "gunicorn"] = "uvicorn"
    # CORS 允许的前端源：逗号分隔字符串（避免 list 字段被 dotenv 当 JSON 解析）
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173"
    )

    # ── 鉴权（外部 API 解析 Bearer token → user_id）────────────────────
    # 验 token 的完整 URL；AUTH_DISABLED=false 时必填
    auth_introspect_url: str = ""
    # GET：仅带 Authorization；POST：另发 JSON {"token": "..."}
    auth_introspect_method: Literal["GET", "POST"] = "GET"
    # 响应 JSON 中用户字段（支持点路径，如 data.user_id）
    auth_user_id_field: str = "user_id"
    auth_timeout_seconds: float = 5.0
    auth_cache_ttl_seconds: float = 60.0
    # True：跳过外部鉴权，固定使用 auth_dev_user_id（仅本地/测试；生产必须 false）
    auth_disabled: bool = False
    auth_dev_user_id: str = "dev-user"

    # ── MCP 安全 ──────────────────────────────────────────────────────
    # stdio 会在 API 进程内拉起子进程，默认关闭；本地调试显式打开
    mcp_stdio_enabled: bool = False
    # 允许的可执行文件 basename（逗号分隔）；启用 stdio 时必填
    mcp_stdio_command_allowlist: str = "npx,uvx,node,python,python3"

    # ── 功能开关 ──────────────────────────────────────────────────────
    # 是否在危险工具（shell / 写文件）前暂停等待人工批准
    enable_hitl: bool = True
    # 日志级别
    log_level: str = "INFO"
    # 单进程同时进行的 SSE / 流式对话上限（0=不限制）
    chat_stream_max_concurrent: int = Field(default=32, ge=0, le=10_000)
    # 获取流式槽位的最长等待秒数；超时返回 429（0=立即失败不排队）
    chat_stream_acquire_timeout_seconds: float = Field(default=1.0, ge=0, le=120)
    # 流式限流作用域：auto=多 worker 用 Redis 全局限流，否则进程内；local/redis 可强制
    chat_stream_limiter: Literal["auto", "local", "redis"] = "auto"
    # 单条用户消息最大字符数
    chat_message_max_chars: int = Field(default=32_000, ge=1, le=2_000_000)
    # SQLAlchemy 异步连接池（与流式并发匹配；0=使用驱动默认）
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_timeout: float = Field(default=30.0, ge=1, le=600)
    # 连接回收秒数；0=不回收（依赖 pool_pre_ping）
    db_pool_recycle: int = Field(default=1800, ge=0, le=86_400)

    # ── 运行时资源生命周期 ────────────────────────────────────────────
    # 进程内 Compiled Agent 缓存上限；淘汰时顺带清构建锁（Skills 物化按内容寻址保留）
    agent_cache_max_size: int = Field(default=32, ge=1, le=10_000)
    # 每个方法论最多保留的历史快照数（仍被会话引用的版本不会删）
    methodology_revision_keep: int = Field(default=20, ge=1, le=10_000)
    # Skills 物化 GC：``.complete`` 超过该天数未刷新则删除（0=禁用）
    skills_gc_max_age_days: float = Field(default=14.0, ge=0, le=3650)
    # Skills 物化 GC：残留临时目录超过该小时数则删除
    skills_gc_tmp_max_age_hours: float = Field(default=1.0, ge=0.01, le=720)
    # 上传技能包 zip：压缩体积上限（字节）
    skill_package_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    # 上传技能包：解压后体积上限（字节）
    skill_package_max_uncompressed_bytes: int = Field(
        default=32 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024
    )
    skill_package_max_files: int = Field(default=80, ge=1, le=500)
    skill_package_max_depth: int = Field(default=4, ge=1, le=12)
    # 附属文件后缀白名单（逗号分隔）；SKILL.md 始终允许
    skill_package_allowed_suffixes: str = (
        ".md,.py,.sh,.json,.yaml,.yml,.txt,.toml,.csv"
    )
    # Skills GC 间隔（小时）；0=后台不跑 Skills（可用 ``python -m deepagents_app.services.infra.gc``）
    # 与 content_blob 共用一个 PeriodicTask；各自 Redis 单飞，不随 API_WORKERS 放大
    skills_gc_interval_hours: float = Field(default=24.0, ge=0, le=720)
    # content_blob 孤儿 GC 间隔（小时）；0=后台不跑 blob
    content_blob_gc_interval_hours: float = Field(default=24.0, ge=0, le=720)
    # Fernet 密钥（url-safe base64）或任意口令；用于加密模型 api_key
    # 生产必须设置；未设置且未允许 insecure 时启动/加密会失败
    secrets_encryption_key: str | None = None
    # 轮转用旧密钥：逗号分隔，解密时在主密钥失败后依次尝试
    secrets_encryption_previous_keys: str = ""
    # True：允许在未配置 SECRETS_ENCRYPTION_KEY 时使用固定开发密钥（仅本地）
    secrets_allow_insecure_dev_key: bool = False
    # /health 探活结果短缓存（秒）；0=每次实时探测；仅缓存成功结果
    health_cache_ttl_seconds: float = Field(default=2.0, ge=0, le=60)

    @field_validator(
        "workspace_dir",
        "memory_file",
        mode="before",
    )
    @classmethod
    def _resolve_path(cls, value: str | Path) -> Path:
        """相对路径一律相对项目根解析，保证启动目录无关。"""
        path = Path(value)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    def ensure_directories(self) -> None:
        """创建运行时必需的目录（幂等）。真实用户目录在 workspace/users/<hash>/。"""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def cors_origin_list(self) -> list[str]:
        """解析 ``cors_origins`` 为前端源列表。"""
        text = (self.cors_origins or "").strip()
        if not text:
            return ["http://localhost:5173", "http://127.0.0.1:5173"]
        if text.startswith("["):
            import json

            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ValueError("cors_origins JSON 必须是数组")
            return [str(x).strip() for x in parsed if str(x).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置；测试场景可 ``get_settings.cache_clear()`` 后重建。"""
    return Settings()


def settings_with(**overrides: Any) -> Settings:
    """
    基于单例复制一份覆盖后的 Settings（不改动缓存中的原对象）。

    例：``settings_with(enable_hitl=True)``
    """
    return get_settings().model_copy(update=overrides)
