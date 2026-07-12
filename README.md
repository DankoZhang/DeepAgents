# DeepAgents 演示框架

基于 [LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview)（`deepagents`）搭建的**完整可运行示例**：主 Agent 负责调度，三个专业子 Agent 分别处理文档、计算机操作与智能问答，并演示 Middleware、Memory、Skills、Filesystem Backend、Permissions、Checkpointer、Human-in-the-loop 等核心能力。

## 架构一览

```
用户
  │
  ▼
┌─────────────────────────────────────────┐
│  Supervisor（主 Agent）                   │
│  - system_prompt 调度策略                 │
│  - write_todos / task（deepagents 内置）  │
│  - Logging / Timing / Audit Middleware    │
│  - Memory(AGENTS.md) + Skills             │
│  - FilesystemBackend + Permissions        │
└────────────┬────────────────────────────┘
             │ task(subagent_type=…)
     ┌───────┼──────────────┐
     ▼       ▼              ▼
 document-  computer-     qa-expert
  writer    operator
 (写文档)   (文件/Shell)   (知识问答)
```

| 组件 | 路径 | 说明 |
|------|------|------|
| 工厂组装 | `deepagents_app/factory.py` | `create_deep_agent(...)` 一站式装配 |
| 主提示词 | `deepagents_app/supervisor/` | 调度策略与路由表 |
| 子 Agent | `deepagents_app/subagents/` | 三个 SubAgent 规格 |
| 工具 | `deepagents_app/tools/` | 文档 / 计算机 / 问答工具 |
| 中间件 | `deepagents_app/middleware/` | 日志、计时、审计 |
| Skills | `deepagents_app/skills/*/SKILL.md` | 渐进披露领域知识 |
| Memory | `AGENTS.md` | 启动即加载的行为准则 |
| 工作区 | `workspace/` | Agent 可读写沙箱 |

## 快速开始

```bash
cd Agents-Project/DeepAgents

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY 或 ANTHROPIC_API_KEY

python main.py
# 或单次提问
python main.py -q "什么是 Deep Agents 的 Middleware？"
```

兼容第三方 OpenAI API（如 DeepSeek）时：

```env
MODEL_PROVIDER=openai_compatible
OPENAI_BASE_URL=https://api.deepseek.com/v1
MODEL_NAME=deepseek-chat
OPENAI_API_KEY=sk-...
```

## 已演示的 Deep Agents 能力

1. **主从调度**：Supervisor + `task` 委派三个专业子 Agent  
2. **自定义 Middleware**：`wrap_model_call` / `wrap_tool_call` 做日志、计时、审计  
3. **Filesystem Backend**：本地 `workspace/` 作为虚拟文件系统根  
4. **Permissions**：拒绝写入 `/audit/**`，其余允许  
5. **Memory**：`AGENTS.md` 注入长期行为准则  
6. **Skills**：`SKILL.md` 渐进披露  
7. **Checkpointer**：多轮 `thread_id` 状态（优先 Sqlite，回退内存）  
8. **HITL**：`ENABLE_HITL=true` 或 `python main.py --hitl`，危险工具前暂停  
9. **HarnessProfile**：定制默认 `general-purpose` 子 Agent 描述  

## 目录结构

```
DeepAgents/
├── main.py                      # CLI 入口
├── AGENTS.md                    # Memory
├── requirements.txt
├── .env.example
├── examples/quickstart.py
├── deepagents_app/
│   ├── factory.py               # 核心：create_deep_agent 组装
│   ├── config.py
│   ├── models.py
│   ├── backends.py
│   ├── supervisor/
│   ├── subagents/
│   ├── tools/
│   ├── middleware/
│   └── skills/
└── workspace/                   # 运行时沙箱（自动创建）
```

## 试用提示词

- 「请解释 Deep Agents 里 Memory 和 Skills 的区别」（走 `qa-expert`）  
- 「写一份本项目的 README 并保存」（走 `document-writer`）  
- 「列出 workspace 里有什么文件」（走 `computer-operator`）  
- 「先查一下 Middleware 是什么，再写一篇介绍文档保存」（主 Agent 串行/并行调度）  

## 说明

本仓库是**教学与脚手架**性质：Shell 白名单、路径沙箱均为演示级。生产环境请改用官方 Sandbox Backend，并加强密钥脱敏、审计与 HITL 策略。
