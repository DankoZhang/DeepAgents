"""
Deep Agent 共享组装工具
======================

供 ``agent_factory`` 复用的底层能力：
checkpointer / HITL / permissions / HarnessProfile / workspace 同步。

方法论驱动的完整 Agent 组装见 ``deepagents_app.services.agent_factory``。
"""

from __future__ import annotations  # 允许类型注解使用未定义的前向引用（如字符串形式）

import logging  # 标准库日志，用于输出 checkpointer / profile 注册结果

# 从 deepagents 引入：文件系统权限、通用子 Agent 配置、Harness 配置与注册函数
from deepagents import (
    FilesystemPermission,  # 声明式路径权限规则（allow/deny + 操作类型）
    GeneralPurposeSubagentProfile,  # 自动注入的 general-purpose 子 Agent 配置项
    HarnessProfile,  # 按 provider 定制 deepagents Harness 行为的配置容器
    register_harness_profile,  # 将 HarnessProfile 注册到指定 provider 键上
)
from langgraph.checkpoint.memory import InMemorySaver  # 进程内 checkpointer（Redis 不可用时回退）

from deepagents_app.config import Settings  # 应用配置（HITL、Redis、模型、路径等）

logger = logging.getLogger(__name__)  # 本模块专用 logger，名称随包路径变化

# 对外公开的符号列表（from deepagents_app.factory import * 时只导出这些）
__all__ = [
    "build_permissions",  # 构建 FilesystemBackend 路径权限规则
    "build_interrupt_on",  # 构建危险工具人工审批（HITL）映射
    "build_checkpointer",  # 构建 LangGraph 会话状态持久化组件
    "configure_general_purpose_profile",  # 注册 general-purpose 子 Agent 的 HarnessProfile
    "sync_memory_and_skills_into_workspace",  # 把 AGENTS.md / skills 同步进 workspace
]


def build_permissions() -> list[FilesystemPermission]:
    """
    声明式路径权限（first-match-wins）。

    示例策略：
    - 允许读写整个 workspace（backend root 映射为 /）
    - 拒绝直接改写审计日志（防止 Agent 篡改证据）
    """
    return [  # 返回权限规则列表；匹配顺序：先匹配先生效
        FilesystemPermission(  # 第一条：保护审计目录
            paths=["/audit/**"],  # 匹配虚拟根下 audit 及其所有子路径
            operations=["write"],  # 仅约束写操作（读仍可按后续规则放行）
            mode="deny",  # 拒绝写 audit，防止 Agent 篡改证据链
        ),
        FilesystemPermission(  # 第二条：放开其余路径
            paths=["/**"],  # 匹配 workspace 虚拟根下全部路径
            operations=["read", "write"],  # 允许读与写
            mode="allow",  # 允许上述操作
        ),
    ]


def build_interrupt_on(settings: Settings) -> dict[str, bool] | None:
    """危险工具人工审批配置；关闭 HITL 时返回 None。"""
    if not settings.enable_hitl:  # 配置关闭 HITL 时不注入任何中断规则
        return None  # create_deep_agent 收到 None 表示不启用人工审批
    return {  # 工具名 -> True：调用前必须人工确认
        "run_shell_command": True,  # shell 执行：高风险，需审批
        "write_workspace_file": True,  # 写 workspace 文件：需审批
        "write_file": True,  # 写文件工具：需审批
        "edit_file": True,  # 编辑文件工具：需审批
        "execute": True,  # 通用执行类工具：需审批
    }


def build_checkpointer(settings: Settings):
    """
    构建 checkpointer，用于多轮对话的 thread 级状态持久化。

    优先 ``langgraph-checkpoint-redis``（跨进程 / 跨服务）。
    ``require_redis_checkpointer=True`` 时 Redis 不可用则抛错；
    否则回退 ``InMemorySaver``（进程内有效，适合本地演示）。

    注意：RedisSaver 需要 Redis 8+（自带 RedisJSON/RediSearch）或 Redis Stack。
    """
    try:  # 优先尝试 Redis 持久化（多 worker / 重启后仍可恢复 thread 状态）
        from langgraph.checkpoint.redis import RedisSaver  # 延迟导入，避免未安装时拖垮整个模块

        saver = RedisSaver(redis_url=settings.redis_url)  # 用配置中的 Redis URL 创建 saver
        saver.setup()  # 初始化 Redis 侧索引/结构（首次或结构变更时需要）
        logger.info("Checkpointer: RedisSaver -> %s", settings.redis_url)  # 记录成功使用 Redis
        return saver  # 返回可用的 Redis checkpointer
    except Exception as exc:  # noqa: BLE001  # 捕获连接失败、缺依赖、缺少 RedisJSON 等一切失败
        if settings.require_redis_checkpointer:  # 生产强制 Redis：不可回退内存
            raise RuntimeError(  # 转为明确错误，阻止应用在无持久化状态下继续
                f"Redis checkpointer 不可用（REQUIRE_REDIS_CHECKPOINTER=true）：{exc}"
            ) from exc  # 保留原始异常链，便于排查根因
        logger.warning(  # 开发/演示模式：告警后降级，不中断启动
            "使用 InMemorySaver（Redis 不可用：%s）。"
            "多 worker / 重启后对话状态会丢失；生产请设 REQUIRE_REDIS_CHECKPOINTER=true",
            exc,  # 把失败原因写进日志
        )
        return InMemorySaver()  # 进程内内存 checkpointer：同进程多轮有效，重启即丢


def configure_general_purpose_profile(settings: Settings) -> None:
    """
    通过 HarnessProfile 定制自动注入的 ``general-purpose`` 子 Agent。

    deepagents 默认会额外挂一个通用子 Agent 作为兜底。
    Profile 按 provider 键注册；未知 provider 时安静跳过。
    """
    provider_keys = {  # 应用内部 provider 名 -> deepagents Harness 注册键
        "openai": "openai",  # 官方 OpenAI 直接映射
        "openai_compatible": "openai",  # 兼容接口（如本地网关）也挂到 openai profile
        "anthropic": "anthropic",  # Anthropic 单独一套 profile 键
    }
    key = provider_keys.get(settings.model_provider)  # 按当前配置取注册键；未知则 None
    if not key:  # 未识别的 provider：不注册，避免误绑错误键
        return  # 安静退出，不影响后续 create_deep_agent

    profile = HarnessProfile(  # 构造要注册的 Harness 配置
        general_purpose_subagent=GeneralPurposeSubagentProfile(  # 只定制通用子 Agent 段
            enabled=True,  # 保持启用：作为专业子 Agent 都不合适时的兜底
            description=(  # 给主 Agent 选人用的简介：强调“仅当专业子 Agent 都不合适”
                "通用兜底子 Agent。仅当 document-writer / computer-operator / "
                "qa-expert 都不适合时使用；优先选择专业子 Agent。"
            ),
            system_prompt=(  # 通用子 Agent 自身系统提示：任务做完就交回简洁结论
                "你是通用助手。完成主 Agent 分配的杂项任务后，"
                "返回简洁结论。默认使用简体中文。"
            ),
        ),
    )
    try:  # 注册可能因版本/键冲突失败，失败时降级为警告
        register_harness_profile(key, profile)  # 按 provider 键注册（如 openai / anthropic）
        register_harness_profile(f"{key}:{settings.model_name}", profile)  # 再按 provider:model 细粒度注册
        logger.info("已注册 HarnessProfile：%s / %s:%s", key, key, settings.model_name)  # 记录双键注册成功
    except Exception as exc:  # noqa: BLE001  # 注册失败不阻断启动
        logger.warning("HarnessProfile 注册失败（可忽略）：%s", exc)  # 打警告后继续用默认行为


def sync_memory_and_skills_into_workspace(settings: Settings) -> None:
    """
    把项目级 AGENTS.md 与 skills/ 同步到 workspace，供 FilesystemBackend 读取。

    原因：FilesystemBackend 的虚拟根是 workspace；memory/skills 路径若写
    ``/AGENTS.md``，实际会读 ``workspace/AGENTS.md``。
    """
    import shutil  # 延迟导入：仅本函数需要文件复制/目录树操作

    src_memory = settings.memory_file  # 项目侧记忆文件源路径（通常为 AGENTS.md）
    dst_memory = settings.workspace_dir / "AGENTS.md"  # workspace 内目标路径（虚拟根可见）
    if src_memory.exists():  # 源文件存在才复制，避免无文件时报错
        shutil.copy2(src_memory, dst_memory)  # copy2 保留元数据；覆盖 workspace 旧版 AGENTS.md

    src_skills = settings.skills_dir  # 项目侧 skills 目录源路径
    dst_skills = settings.workspace_dir / "skills"  # workspace 内 skills 目标目录
    if src_skills.exists():  # 源目录存在才同步
        if dst_skills.exists():  # 目标已存在则先清空，保证与源目录一致（避免残留旧 skill）
            shutil.rmtree(dst_skills)  # 递归删除旧 skills 树
        shutil.copytree(src_skills, dst_skills)  # 整树复制到 workspace/skills
