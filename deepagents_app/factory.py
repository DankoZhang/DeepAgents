"""
Deep Agent 工厂（核心组装点）
==============================

把「模型 + 主提示词 + 子 Agent + Backend + Memory + Skills + Middleware +
Checkpointer + HITL」组装成一个可 ``invoke`` / ``ainvoke`` / ``stream`` 的图。

对应 deepagents 官方能力一览（本工厂均有演示入口）：

| 能力 | 本文件用法 |
|------|------------|
| 子 Agent 委派 | ``subagents=build_all_subagents()``（YAML 配置） |
| 虚拟文件系统 | ``backend=FilesystemBackend(...)`` |
| Memory | ``memory=[AGENTS.md 路径]`` |
| Skills | 挂在各子 Agent ``skills=[专属 source]`` |
| 自定义 Middleware | ``middleware=[Logging, Timing, Audit]`` |
| Human-in-the-loop | ``interrupt_on={...}``（开关控制） |
| 多轮持久化 | ``checkpointer=RedisSaver`` |
| 路径权限 | ``permissions=[...]`` |
"""

from __future__ import annotations

import logging
from typing import Any

from deepagents import (
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from langgraph.checkpoint.memory import InMemorySaver

from deepagents_app.backends import build_filesystem_backend
from deepagents_app.config import Settings, get_settings
from deepagents_app.middleware import AuditMiddleware, LoggingMiddleware, TimingMiddleware
from deepagents_app.models import build_chat_model
from deepagents_app.subagents import build_all_subagents
from deepagents_app.supervisor import SUPERVISOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def _build_middleware(settings: Settings) -> list[Any]:
    """组装主 Agent 自定义 middleware 列表。"""
    if not settings.enable_custom_middleware:
        return []
    return [
        LoggingMiddleware(),
        TimingMiddleware(),
        AuditMiddleware(),
    ]


def _build_permissions() -> list[FilesystemPermission]:
    """
    声明式路径权限（first-match-wins）。

    示例策略：
    - 允许读写整个 workspace（backend root 映射为 /）
    - 拒绝直接改写审计日志（防止 Agent 篡改证据）
    """
    return [
        FilesystemPermission(
            paths=["/audit/**"],
            operations=["write"],
            mode="deny",
        ),
        FilesystemPermission(
            paths=["/**"],
            operations=["read", "write"],
            mode="allow",
        ),
    ]


def _build_interrupt_on(settings: Settings) -> dict[str, bool] | None:
    """危险工具人工审批配置；关闭 HITL 时返回 None。"""
    if not settings.enable_hitl:
        return None
    # True 表示该工具每次调用前都 interrupt
    return {
        "run_shell_command": True,
        "write_workspace_file": True,
        "write_file": True,
        "edit_file": True,
        "execute": True,
    }


def _build_checkpointer(settings: Settings):
    """
    构建 checkpointer，用于多轮对话的 thread 级状态持久化。

    优先 ``langgraph-checkpoint-redis``（跨进程 / 跨服务）。
    ``require_redis_checkpointer=True`` 时 Redis 不可用则抛错；
    否则回退 ``InMemorySaver``（进程内有效，适合本地演示）。

    注意：RedisSaver 需要 Redis 8+（自带 RedisJSON/RediSearch）或 Redis Stack。
    """
    try:
        from langgraph.checkpoint.redis import RedisSaver

        # 不用 from_conn_string 上下文管理器：工厂返回的 saver 需与 Agent 同生命周期
        saver = RedisSaver(redis_url=settings.redis_url)
        saver.setup()
        logger.info("Checkpointer: RedisSaver -> %s", settings.redis_url)
        return saver
    except Exception as exc:  # noqa: BLE001
        if settings.require_redis_checkpointer:
            raise RuntimeError(
                f"Redis checkpointer 不可用（REQUIRE_REDIS_CHECKPOINTER=true）：{exc}"
            ) from exc
        logger.warning(
            "使用 InMemorySaver（Redis 不可用：%s）。"
            "多 worker / 重启后对话状态会丢失；生产请设 REQUIRE_REDIS_CHECKPOINTER=true",
            exc,
        )
        return InMemorySaver()


def _configure_general_purpose_profile(settings: Settings) -> None:
    """
    通过 HarnessProfile 定制自动注入的 ``general-purpose`` 子 Agent。

    deepagents 默认会额外挂一个通用子 Agent 作为兜底。
    这里把它的描述改成中文，并强调「仅在三个专业子 Agent 都不合适时使用」。
    Profile 按 provider 键注册；未知 provider 时安静跳过。
    """
    provider_keys = {
        "openai": "openai",
        "openai_compatible": "openai",
        "anthropic": "anthropic",
    }
    key = provider_keys.get(settings.model_provider)
    if not key:
        return

    profile = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(
            enabled=True,
            description=(
                "通用兜底子 Agent。仅当 document-writer / computer-operator / "
                "qa-expert 都不适合时使用；优先选择专业子 Agent。"
            ),
            system_prompt=(
                "你是通用助手。完成主 Agent 分配的杂项任务后，"
                "返回简洁结论。默认使用简体中文。"
            ),
        ),
    )
    try:
        register_harness_profile(key, profile)
        # 同时按 provider:model 注册，覆盖更精确
        register_harness_profile(f"{key}:{settings.model_name}", profile)
        logger.info("已注册 HarnessProfile：%s / %s:%s", key, key, settings.model_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("HarnessProfile 注册失败（可忽略）：%s", exc)


def _sync_memory_and_skills_into_workspace(settings: Settings) -> None:
    """
    把项目级 AGENTS.md 与 skills/ 同步到 workspace，供 FilesystemBackend 读取。

    原因：FilesystemBackend 的虚拟根是 workspace；memory/skills 路径若写
    ``/AGENTS.md``，实际会读 ``workspace/AGENTS.md``。
    """
    import shutil

    # Memory
    src_memory = settings.memory_file
    dst_memory = settings.workspace_dir / "AGENTS.md"
    if src_memory.exists():
        shutil.copy2(src_memory, dst_memory)

    # Skills：整个目录树复制
    # 期望结构（与 deepagents SkillsMiddleware 一层扫描一致）：
    #   skills/<subagent-name>/<skill-name>/SKILL.md
    src_skills = settings.skills_dir
    dst_skills = settings.workspace_dir / "skills"
    if src_skills.exists():
        if dst_skills.exists():
            shutil.rmtree(dst_skills)
        shutil.copytree(src_skills, dst_skills)


def build_deep_agent(settings: Settings | None = None):
    """
    创建完整的 Deep Agent 图。

    Returns:
        LangGraph ``CompiledStateGraph``，可直接::

            agent.invoke(
                {"messages": [{"role": "user", "content": "..."}]},
                config={"configurable": {"thread_id": "session-1"}},
            )
    """
    settings = settings or get_settings()
    model = build_chat_model(settings)
    backend = build_filesystem_backend(settings)
    subagents = build_all_subagents(settings.subagents_config)
    middleware = _build_middleware(settings)
    interrupt_on = _build_interrupt_on(settings)
    checkpointer = _build_checkpointer(settings)
    permissions = _build_permissions()

    _configure_general_purpose_profile(settings)

    # Memory / Skills：FilesystemBackend 虚拟根 = workspace，
    # 因此先把项目级 AGENTS.md / skills 同步进 workspace，再传虚拟路径。
    # Skills 挂在各子 Agent 的 skills 字段（见 subagents/*），主 Agent 不加载。
    _sync_memory_and_skills_into_workspace(settings)

    memory_paths = ["/AGENTS.md"] if (settings.workspace_dir / "AGENTS.md").exists() else None

    logger.info(
        "组装 Deep Agent：subagents=%s middleware=%d hitl=%s memory=%s skills=%s",
        [s["name"] for s in subagents],
        len(middleware),
        bool(interrupt_on),
        memory_paths,
        {s["name"]: s.get("skills") for s in subagents},
    )

    agent = create_deep_agent(
        model=model,
        system_prompt=SUPERVISOR_SYSTEM_PROMPT,
        subagents=subagents,
        backend=backend,
        middleware=middleware,
        memory=memory_paths,
        permissions=permissions,
        interrupt_on=interrupt_on,
        checkpointer=checkpointer,
        name="deepagents-supervisor",
    )
    return agent