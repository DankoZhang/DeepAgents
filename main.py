#!/usr/bin/env python3
"""
DeepAgents CLI
==============

按数据库方法论动态组装 Agent（需 PostgreSQL；启动时会幂等 seed）。

用法::

    cp .env.example .env   # 填入 API Key
    docker compose up -d   # PostgreSQL + Redis
    python main.py                              # 默认 demo_deepagents
    python main.py --methodology demo_deepagents
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
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepagents_app.config import get_settings  # noqa: E402
from deepagents_app.ownership import demo_methodology_id_for_user  # noqa: E402

console = Console()

DEFAULT_CLI_USER = "cli-user"


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
    from deepagents_app.services.chat import extract_final_text

    return extract_final_text(result) or "(未获得模型文本回复，请查看日志)"


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


def run_once(agent: Any, text: str, thread_id: str, *, user_id: str) -> None:
    from deepagents_app.ownership import checkpoint_thread_id

    config = {"configurable": {"thread_id": checkpoint_thread_id(user_id, thread_id)}}
    console.print(f"[dim]thread={thread_id} user={user_id}[/dim]")
    with console.status("[bold cyan]Agent 思考 / 调度中…[/bold cyan]"):
        result = agent.invoke(
            {"messages": [{"role": "user", "content": text}]},
            config=config,
        )
    result = _handle_interrupt(agent, config, result)
    final = _extract_final_text(result)
    console.print(Panel(Markdown(final), title="Supervisor 回复", border_style="green"))


def interactive_loop(agent: Any, thread_id: str, *, title: str, user_id: str) -> None:
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
            run_once(agent, text, current_thread, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]执行失败：{exc}[/red]")
            logging.exception("run_once failed")


def _build_agent_methodology(settings: Any, methodology_id: str, user_id: str) -> Any:
    from deepagents_app.db.seed import ensure_user_bootstrap
    from deepagents_app.db.session import get_session_factory, migrate_db
    from deepagents_app.services.agent_factory import build_agent_from_methodology
    from deepagents_app.services.revisions import flush_cache_invalidations

    migrate_db()
    factory = get_session_factory()
    db = factory()
    try:
        ensure_user_bootstrap(db, user_id)
        db.commit()
        flush_cache_invalidations(db)
        return build_agent_from_methodology(
            db,
            methodology_id,
            owner_user_id=user_id,
            settings=settings,
        )
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
        "--user",
        default=None,
        help="CLI 用户 id（默认 AUTH_DEV_USER_ID 或 cli-user）",
    )
    parser.add_argument(
        "--methodology",
        default=None,
        help="方法论 ID（默认当前用户的 demo 方法论）",
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
    user_id = args.user or settings.auth_dev_user_id or DEFAULT_CLI_USER
    methodology_id = args.methodology or demo_methodology_id_for_user(user_id)

    try:
        console.print(
            f"[dim]正在按方法论组装 Agent：{methodology_id}（user={user_id}）…[/dim]"
        )
        agent = _build_agent_methodology(settings, methodology_id, user_id)
        title = f"方法论驱动：`{methodology_id}`"
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]组装失败：{exc}[/red]")
        console.print(
            "请检查 .env（API Key / DATABASE_URL），并确认 docker compose 已启动。"
        )
        return 1

    if args.query:
        run_once(agent, args.query, args.thread, user_id=user_id)
    else:
        interactive_loop(agent, args.thread, title=title, user_id=user_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
