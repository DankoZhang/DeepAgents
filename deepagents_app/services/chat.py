"""
聊天服务
========

按 thread 加载 Conversation → Agent Factory（锁定 methodology_version）→
invoke / resume / 读取 checkpointer 历史。
"""

from __future__ import annotations  # 启用延迟注解求值，便于前向引用类型

import logging  # 标准日志模块
from typing import Any  # 任意类型占位，用于消息/结果等异构结构

from sqlalchemy.orm import Session  # SQLAlchemy 数据库会话类型

from deepagents_app.services.agent_factory import build_agent_from_methodology  # 按方法论构建 agent
from deepagents_app.services.conversation import get_conversation_by_thread  # 按 thread_id 查会话

logger = logging.getLogger(__name__)  # 本模块专用 logger


def _normalize_content(content: Any) -> str:
    """统一把 str / multimodal block 列表转为纯文本。"""
    if content is None:  # 空内容视为空字符串
        return ""
    if isinstance(content, str):  # 已是纯文本则直接返回
        return content
    if isinstance(content, list):  # multimodal：block 列表
        texts = [
            # dict block 取 text 字段；否则转成字符串
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content  # 遍历每个内容块
        ]
        return "\n".join(t for t in texts if t)  # 过滤空段后用换行拼接
    return str(content)  # 其它类型统一转成字符串


def _msg_role(msg: Any) -> str:
    """LangChain Message.type / OpenAI role → 前端 role。"""
    # 优先取 Message.type；dict 则取 role
    raw = getattr(msg, "type", None) or (
        msg.get("role") if isinstance(msg, dict) else None
    )
    raw = str(raw or "unknown")  # 缺失时记为 unknown
    mapping = {
        "human": "user",  # LangChain human → 前端 user
        "ai": "assistant",  # LangChain ai → 前端 assistant
        "assistant": "assistant",  # 已是 assistant 保持不变
        "user": "user",  # 已是 user 保持不变
        "system": "system",  # 系统消息
        "tool": "tool",  # 工具消息
    }
    return mapping.get(raw, raw)  # 未映射则原样返回


def serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """将 LangChain / dict 消息转为前端可用结构。"""
    out: list[dict[str, Any]] = []  # 序列化结果列表
    for msg in messages or []:  # 空列表时安全遍历
        role = _msg_role(msg)  # 归一化角色名
        content = _normalize_content(
            # 对象取 content 属性，dict 取 content 键
            getattr(msg, "content", None)
            or (msg.get("content") if isinstance(msg, dict) else None)
        )
        name = getattr(msg, "name", None) or (
            # 工具名等可选字段
            msg.get("name") if isinstance(msg, dict) else None
        )
        # 跳过空内容的中间 tool / 系统噪声（保留有文本的）
        if not content and role not in {"user", "assistant"}:
            continue  # 无文本且非主对话角色则丢弃
        out.append({"role": role, "content": content, "name": name})  # 追加前端结构
    return out  # 返回可 JSON 序列化的消息列表


def extract_final_text(result: dict[str, Any]) -> str:
    """从 agent.invoke 结果取出最后一条 AI 文本。"""
    messages = result.get("messages") or []  # 取消息列表，缺省为空
    for msg in reversed(messages):  # 从后往前找最新回复
        role = _msg_role(msg)  # 归一化角色
        content = _normalize_content(
            # 兼容 Message 对象与 dict
            getattr(msg, "content", None)
            or (msg.get("content") if isinstance(msg, dict) else None)
        )
        if role == "assistant" and content:  # 找到有文本的助手消息
            return content  # 返回该条文本作为最终回复
    return ""  # 没有可用 AI 文本则返回空串


def _pack_result(
    *,
    thread_id: str,  # 会话线程 ID
    result: dict[str, Any],  # agent.invoke 原始结果
    methodology_id: str,  # 方法论 ID
    methodology_version: int,  # 锁定的方法论版本号
) -> dict[str, Any]:
    """统一 chat / resume 响应结构；``__interrupt__`` 表示 HITL 暂停。"""
    interrupts = result.get("__interrupt__")  # LangGraph HITL 中断载荷
    return {
        "thread_id": thread_id,  # 回传线程 ID
        "reply": extract_final_text(result),  # 提取最终助手文本
        "interrupted": bool(interrupts),  # 是否处于 HITL 暂停
        "interrupt": str(interrupts) if interrupts else None,  # 中断详情或 None
        "methodology_id": methodology_id,  # 会话绑定的方法论
        "methodology_version": methodology_version,  # 会话锁定的版本
    }


def chat(
    db: Session,  # 数据库会话
    *,
    thread_id: str,  # 目标会话线程
    message: str,  # 本轮用户输入
) -> dict[str, Any]:
    """
    执行一轮对话。

    Returns:
        {
          "thread_id": ...,
          "reply": "...",
          "interrupted": bool,
          "interrupt": ...,
          "methodology_id": ...,
          "methodology_version": ...,
        }
    """
    conversation = get_conversation_by_thread(db, thread_id)  # 加载会话记录
    if conversation is None:  # 线程不存在
        raise LookupError(f"会话不存在：thread_id={thread_id}")  # 向上抛出查找错误

    # 必须按会话创建时的 version 构建，避免方法论升级影响进行中的对话
    agent = build_agent_from_methodology(
        db,  # 传入 DB 以读方法论/中间件配置
        conversation.methodology_id,  # 会话绑定的方法论
        version=conversation.methodology_version,  # 锁定创建时的版本
    )
    # LangGraph 用 thread_id 隔离多轮 checkpointer 状态
    config = {"configurable": {"thread_id": thread_id}}  # 运行时配置
    logger.info(
        "chat thread=%s methodology=%s v%s",  # 结构化日志模板
        thread_id,  # 当前线程
        conversation.methodology_id,  # 方法论 ID
        conversation.methodology_version,  # 方法论版本
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": message}]},  # 本轮用户消息
        config=config,  # 带 thread_id 的配置
    )
    return _pack_result(
        thread_id=thread_id,  # 回传线程
        result=result,  # invoke 原始结果
        methodology_id=conversation.methodology_id,  # 方法论 ID
        methodology_version=conversation.methodology_version,  # 方法论版本
    )


def resume_chat(
    db: Session,  # 数据库会话
    *,
    thread_id: str,  # 待恢复的线程
    approve: bool = True,  # True 批准工具调用；False 拒绝
) -> dict[str, Any]:
    """
    恢复 HITL 中断的会话。

    approve=True → 批准当前工具调用并继续；
    approve=False → 拒绝并结束本轮。
    """
    from langgraph.types import Command  # 延迟导入：用于 resume 决策命令

    conversation = get_conversation_by_thread(db, thread_id)  # 加载会话
    if conversation is None:  # 会话不存在
        raise LookupError(f"会话不存在：thread_id={thread_id}")  # 抛出查找错误

    agent = build_agent_from_methodology(
        db,  # DB 会话
        conversation.methodology_id,  # 方法论 ID
        version=conversation.methodology_version,  # 锁定版本
    )
    config = {"configurable": {"thread_id": thread_id}}  # checkpointer 线程隔离
    decision_type = "approve" if approve else "reject"  # 映射为 LangGraph 决策类型
    logger.info("chat resume thread=%s decision=%s", thread_id, decision_type)  # 记录恢复决策
    result = agent.invoke(
        Command(resume={"decisions": [{"type": decision_type}]}),  # 提交 HITL 决策并继续
        config=config,  # 同一 thread 上恢复
    )
    return _pack_result(
        thread_id=thread_id,  # 回传线程
        result=result,  # resume 后的 invoke 结果
        methodology_id=conversation.methodology_id,  # 方法论 ID
        methodology_version=conversation.methodology_version,  # 方法论版本
    )


def get_conversation_messages(
    db: Session,  # 数据库会话
    *,
    thread_id: str,  # 要回放的线程
) -> dict[str, Any]:
    """
    从 checkpointer 读取会话历史（供前端聊天页回放）。

    无历史时返回空 messages 列表。
    """
    conversation = get_conversation_by_thread(db, thread_id)  # 加载会话元数据
    if conversation is None:  # 找不到会话
        raise LookupError(f"会话不存在：thread_id={thread_id}")  # 抛出查找错误

    agent = build_agent_from_methodology(
        db,  # DB 会话
        conversation.methodology_id,  # 方法论 ID
        version=conversation.methodology_version,  # 锁定版本（与对话时一致）
    )
    config = {"configurable": {"thread_id": thread_id}}  # 指定 checkpointer 线程
    messages: list[Any] = []  # 默认无历史消息
    interrupted = False  # 默认未处于 HITL 暂停
    interrupt: str | None = None  # 中断详情，无则为 None

    try:
        state = agent.get_state(config)  # 读取 LangGraph 当前状态快照
        values = getattr(state, "values", None) or {}  # 状态中的 values 字典
        if isinstance(values, dict):  # 防御非 dict 形态
            messages = values.get("messages") or []  # 取出消息通道
        # tasks 上挂着未解决的 interrupt（HITL 暂停中）
        tasks = getattr(state, "tasks", None) or ()  # 未完成任务集合
        for task in tasks:  # 扫描每个任务是否带中断
            interrupts = getattr(task, "interrupts", None) or ()  # 任务上的 interrupt 列表
            if interrupts:  # 存在未解决中断
                interrupted = True  # 标记为已中断
                interrupt = str(interrupts)  # 序列化中断信息供前端展示
                break  # 找到一个即可
    except Exception as exc:  # noqa: BLE001  # 读取失败不阻断接口（如无 checkpoint）
        logger.warning("读取会话状态失败 thread=%s: %s", thread_id, exc)  # 记录告警日志

    return {
        "thread_id": thread_id,  # 线程 ID
        "methodology_id": conversation.methodology_id,  # 方法论 ID
        "methodology_version": conversation.methodology_version,  # 方法论版本
        "messages": serialize_messages(messages),  # 序列化为前端消息结构
        "interrupted": interrupted,  # 是否 HITL 暂停中
        "interrupt": interrupt,  # 中断详情或 None
    }
