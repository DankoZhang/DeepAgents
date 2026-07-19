#!/usr/bin/env python3
"""
DeepAgents CLI
==============

默认走 YAML 演示工厂；也可按方法论从数据库动态组装（对齐设计文档）。

用法::

    # 安装依赖后
    cp .env.example .env   # 填入 API Key
    docker compose up -d   # PostgreSQL + Redis
    python main.py

    # 使用数据库中的方法论（需先 python server.py 种子数据，或手动 seed）
    python main.py --methodology demo_deepagents

    # 单次提问
    python main.py -q "什么是 Deep Agents 的 Middleware？"

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

from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepagents_app.config import get_settings  # noqa: E402

console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _extract_final_text(result: dict[str, Any]) -> str:
    messages = result.get("messages") or []
    for msg in reversed(messages):
        role = getattr(msg, "type", None) or (
            msg.get("role") if isinstance(msg, dict) else None
        )
        content = getattr(msg, "content", None) or (
            msg.get("content") if isinstance(msg, dict) else None
        )
        if role in {"ai", "assistant"} and content:
            if isinstance(content, list):
                texts = [
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                ]
                return "\n".join(t for t in texts if t)
            return str(content)
    return "(未获得模型文本回复，请查看日志)"


def _handle_interrupt(
    agent: Any, config: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
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

    try:
        from langgraph.types import Command

        return agent.invoke(
            Command(resume={"decisions": [{"type": "approve"}]}), config=config
        )
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]Resume 失败：{exc}。请检查 deepagents/langgraph 版本。[/yellow]"
        )
        return result


def run_once(agent: Any, text: str, thread_id: str) -> None:
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


def interactive_loop(agent: Any, thread_id: str, *, title: str) -> None:
    console.print(
        Panel(
            f"{title}\n"
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


def _build_agent_yaml(settings: Any) -> Any:
    from deepagents_app.factory import build_deep_agent

    return build_deep_agent(settings)


def _build_agent_methodology(settings: Any, methodology_id: str) -> Any:
    from deepagents_app.db.seed import seed_defaults
    from deepagents_app.db.session import get_session_factory, init_db
    from deepagents_app.services.agent_factory import build_agent_from_methodology

    init_db()
    factory = get_session_factory()
    db = factory()
    try:
        seed_defaults(db)
        db.commit()
        agent = build_agent_from_methodology(db, methodology_id, settings=settings)
        return agent
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeepAgents CLI")
    parser.add_argument("-q", "--query", help="单次提问（不进入交互）")
    parser.add_argument(
        "--thread",
        default=f"session-{uuid.uuid4().hex[:8]}",
        help="LangGraph thread_id，用于多轮状态隔离",
    )
    parser.add_argument(
        "--methodology",
        default=None,
        help="按方法论 ID 从数据库动态组装 Agent（例：demo_deepagents）",
    )
    parser.add_argument(
        "--hitl",
        action="store_true",
        help="强制开启 Human-in-the-loop（覆盖 .env）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    if args.hitl:
        settings.enable_hitl = True

    _setup_logging(settings.log_level)

    try:
        if args.methodology:
            console.print(
                f"[dim]正在按方法论组装 Agent：{args.methodology}…[/dim]"
            )
            agent = _build_agent_methodology(settings, args.methodology)
            title = f"方法论驱动：`{args.methodology}`"
        else:
            console.print(
                "[dim]正在组装 Deep Agent（YAML / 模型 / Middleware / Backend）…[/dim]"
            )
            agent = _build_agent_yaml(settings)
            title = (
                "YAML 演示模式。子 Agent：`document-writer` / "
                "`computer-operator` / `qa-expert`\n"
                "提示：可用 `--methodology demo_deepagents` 切换数据库驱动模式。"
            )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]组装失败：{exc}[/red]")
        console.print(
            "请检查 .env（API Key / DATABASE_URL），并确认 docker compose 已启动。"
        )
        return 1

    if args.query:
        run_once(agent, args.query, args.thread)
    else:
        interactive_loop(agent, args.thread, title=title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
