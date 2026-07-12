#!/usr/bin/env python3
"""
DeepAgents 演示框架 —— CLI 入口
================================

用法::

    # 安装依赖后
    cp .env.example .env   # 填入 API Key
    python main.py

    # 单次提问
    python main.py -q "什么是 Deep Agents 的 Middleware？"

    # 指定 thread（多轮记忆）
    python main.py --thread demo-1

交互命令：
- 直接输入问题回车
- ``/quit`` 或 ``/exit`` 退出
- ``/thread <id>`` 切换会话线程
- ``/help`` 查看帮助
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

# 保证以脚本方式运行时能找到 deepagents_app
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepagents_app.config import get_settings  # noqa: E402
from deepagents_app.factory import build_deep_agent  # noqa: E402

console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # 降低第三方噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _extract_final_text(result: dict[str, Any]) -> str:
    """
    从 agent.invoke 结果中取出最后一条 AI 文本。
    """
    messages = result.get("messages") or []
    for msg in reversed(messages):
        # LangChain message 对象或 dict 都兼容
        role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else None)
        content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
        if role in {"ai", "assistant"} and content:
            if isinstance(content, list):
                # 多模态 content blocks
                texts = [
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                ]
                return "\n".join(t for t in texts if t)
            return str(content)
    return "(未获得模型文本回复，请查看日志)"


def _handle_interrupt(agent: Any, config: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """
    Human-in-the-loop：若图在 interrupt 处暂停，提示用户批准后继续。

    LangGraph 会在 ``result`` / state 中带有 ``__interrupt__`` 信息。
    """
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return result

    console.print(
        Panel(
            "检测到 Human-in-the-loop 中断：危险工具等待批准。\n"
            "输入 [bold]y[/bold] 批准，[bold]n[/bold] 拒绝。",
            title="HITL",
            border_style="yellow",
        )
    )
    console.print(str(interrupts))
    decision = console.input("[HITL] 批准执行？ [y/N] ").strip().lower()
    if decision != "y":
        console.print("[red]已拒绝，结束本轮。[/red]")
        return result

    # 批准后 resume；具体 Command 类型随 langgraph 版本略有差异
    try:
        from langgraph.types import Command

        return agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Resume 失败：{exc}。请检查 deepagents/langgraph 版本。[/yellow]")
        return result


def run_once(agent: Any, text: str, thread_id: str) -> None:
    """
    执行单轮用户输入并打印回复。
    """
    config = {"configurable": {"thread_id": thread_id}}
    console.print(f"[dim]thread={thread_id}[/dim]")
    with console.status("[bold cyan]Agent 思考 / 调度中…[/bold cyan]"):
        result = agent.invoke(
            {"messages": [{"role": "user", "content": text}]},
            config=config,
        )
    result = _handle_interrupt(agent, config, result)
    final = _extract_final_text(result)
    console.print(Panel(Markdown(final), title="Supervisor 回复", border_style="green"))


def interactive_loop(agent: Any, thread_id: str) -> None:
    """
    多轮交互 REPL。
    """
    console.print(
        Panel(
            "DeepAgents 演示框架已启动。\n"
            "子 Agent：`document-writer` / `computer-operator` / `qa-expert`\n"
            "输入问题开始；`/help` 查看命令。",
            title="DeepAgents",
            border_style="cyan",
        )
    )
    current_thread = thread_id

    while True:
        try:
            text = console.input("[bold blue]你 › [/bold blue]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见。")
            break

        if not text:
            continue
        if text in {"/quit", "/exit", "quit", "exit"}:
            console.print("再见。")
            break
        if text == "/help":
            console.print(
                Markdown(
                    "- `/quit` 退出\n"
                    "- `/thread <id>` 切换会话线程\n"
                    "- `/new` 新建随机 thread\n"
                    "- 其他输入作为用户问题发给 Supervisor"
                )
            )
            continue
        if text.startswith("/thread "):
            current_thread = text.split(maxsplit=1)[1].strip() or current_thread
            console.print(f"[green]已切换 thread → {current_thread}[/green]")
            continue
        if text == "/new":
            current_thread = f"session-{uuid.uuid4().hex[:8]}"
            console.print(f"[green]新 thread → {current_thread}[/green]")
            continue

        try:
            run_once(agent, text, current_thread)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]执行失败：{exc}[/red]")
            logging.exception("run_once failed")


def parse_args() -> argparse.Namespace:
    """
    解析参数
    """
    parser = argparse.ArgumentParser(description="DeepAgents 演示框架 CLI")
    parser.add_argument("-q", "--query", help="单次提问（不进入交互）")
    parser.add_argument(
        "--thread",
        default=f"session-{uuid.uuid4().hex[:8]}",
        help="LangGraph thread_id，用于多轮状态隔离",
    )
    parser.add_argument(
        "--hitl",
        action="store_true",
        help="强制开启 Human-in-the-loop（覆盖 .env）",
    )
    return parser.parse_args()


def main() -> int:
    """
    主函数
    """
    args = parse_args()
    settings = get_settings()
    if args.hitl:
        settings.enable_hitl = True

    _setup_logging(settings.log_level)
    console.print("[dim]正在组装 Deep Agent（模型 / 子 Agent / Middleware / Backend）…[/dim]")

    try:
        agent = build_deep_agent(settings)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]组装失败：{exc}[/red]")
        console.print("请检查 .env 中的 API Key 与 MODEL_* 配置。")
        return 1

    if args.query:
        run_once(agent, args.query, args.thread)
    else:
        interactive_loop(agent, args.thread)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
