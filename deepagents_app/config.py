"""
应用配置
========

集中管理模型、路径、功能开关等运行时参数。

设计要点：
- 使用 ``pydantic-settings`` 从环境变量 / ``.env`` 自动加载
- 路径统一解析为绝对路径，避免 cwd 变化导致 workspace 漂移
- 业务代码只依赖 ``get_settings()``，不直接读 ``os.environ``
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：Agents-Project/DeepAgents/
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """DeepAgents 演示框架的全局配置。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 模型 ──────────────────────────────────────────────────────────
    model_provider: Literal["openai", "anthropic", "openai_compatible"] = "openai"
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
    # True：Redis 不可用时直接失败（生产建议开启）；False：回退 InMemorySaver
    require_redis_checkpointer: bool = False

    # PostgreSQL：方法论 / Agent / Tool / Middleware / Conversation 配置库
    database_url: str = (
        "postgresql+psycopg://deepagents:deepagents@localhost:5432/deepagents"
    )

    # FastAPI
    api_host: str = "0.0.0.0"
    api_port: int = 8001

    # ── 鉴权（外部 API 解析 Bearer token → user_id）────────────────────
    # 验 token 的完整 URL；AUTH_DISABLED=false 时必填
    auth_introspect_url: str = ""
    # GET：仅带 Authorization；POST：另发 JSON {"token": "..."}
    auth_introspect_method: Literal["GET", "POST"] = "GET"
    # 响应 JSON 中用户字段（支持点路径，如 data.user_id）
    auth_user_id_field: str = "user_id"
    auth_timeout_seconds: float = 5.0
    auth_cache_ttl_seconds: float = 60.0
    # True：跳过外部鉴权，固定使用 auth_dev_user_id（仅本地/测试）
    auth_disabled: bool = True
    auth_dev_user_id: str = "dev-user"

    # ── 功能开关 ──────────────────────────────────────────────────────
    # 是否在危险工具（shell / 写文件）前暂停等待人工批准
    enable_hitl: bool = False
    # 日志级别
    log_level: str = "INFO"

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
        """创建运行时必需的目录（幂等）。"""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "documents").mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "notes").mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置；测试场景可 ``get_settings.cache_clear()`` 后重建。"""
    settings = Settings()
    settings.ensure_directories()
    return settings
