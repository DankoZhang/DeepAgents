# DeepAgents 方法论平台（后端）

基于 [LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) 的**可配置方法论驱动多 Agent 平台**后端。

支持：

- FastAPI 配置与会话 API（含 SSE 流式聊天）
- PostgreSQL 方法论 / Agent / Tool / Skill / Middleware / 模型目录（按用户隔离）
- Redis（LangGraph checkpoint）多轮会话隔离
- 按方法论动态 `create_deep_agent()` + 进程内 LRU 缓存
- 方法论版本快照（旧会话锁定创建时版本）
- Skills 存数据库，组装时物化到 `workspace/users/<scope>/`

前端仓库：[`../DeepAgents-frontend`](../DeepAgents-frontend)

---

## 平台模式（推荐）

```bash
cd Agents-Project/DeepAgents

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 或：uv sync   （开发工具：uv sync --group dev）

cp .env.example .env
# 编辑 .env，填入 API Key

# 启动 PostgreSQL + Redis
docker compose up -d

# 迁移 schema（部署步骤；API 启动不会自动执行）
python -m deepagents_app.db.migrate

# 启动 FastAPI（默认 http://0.0.0.0:8001）
# 需配置 AUTH_INTROSPECT_URL，或本地设 AUTH_DISABLED=true
python server.py
```

鉴权：请求头带 `Authorization: Bearer <token>`，服务端调用外部 `AUTH_INTROSPECT_URL` 解析出 `user_id`；所有配置与会话按用户隔离。前端进入布局时会调 `POST /api/bootstrap`，幂等写入该用户的默认 Tool / demo 方法论（本地 `AUTH_DISABLED` 时 lifespan 也会预灌 dev 用户）。

Schema 变更请用 Alembic（见 `migrations/README`）。脚本目录是 `migrations/`（由 `alembic.ini` 的 `script_location` 指定）；勿在仓库根再建 `alembic/`，会遮蔽 PyPI 包：

```bash
alembic revision --autogenerate -m "your change"
python -m deepagents_app.db.migrate
```

另开终端启动前端：

```bash
cd Agents-Project/DeepAgents-frontend
npm install
npm run dev
# http://localhost:5173
```

API 文档：http://localhost:8001/docs

主要接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/bootstrap` | 按用户幂等灌种子 |
| POST | `/api/methodology` | 创建方法论 |
| GET | `/api/methodology/list` | 列表（`limit`/`offset`，`X-Total-Count`） |
| POST | `/api/methodology/{id}/publish` | 发布 |
| POST | `/api/agent` | 创建 Agent |
| GET | `/api/tool/list` | 工具注册表 |
| GET | `/api/middleware/list` | 中间件注册表 |
| POST | `/api/conversation` | 创建会话（绑定方法论版本） |
| GET | `/api/conversation/{thread_id}/messages` | 历史消息 |
| POST | `/api/chat` | 聊天（同步 JSON） |
| POST | `/api/chat/stream` | 聊天（SSE） |
| POST | `/api/chat/resume` | HITL 恢复 |

测试：

```bash
python -m pytest tests/ -q
```

---

## CLI（方法论驱动）

需 PostgreSQL（`docker compose up -d`）。默认使用当前用户的 demo 方法论：

```bash
python main.py
python main.py --methodology <methodology_id>
python main.py -q "什么是 Deep Agents 的 Middleware？"
```

兼容第三方 OpenAI API（如 DeepSeek）：

```env
MODEL_PROVIDER=openai_compatible
OPENAI_BASE_URL=https://api.deepseek.com/v1
MODEL_NAME=deepseek-chat
OPENAI_API_KEY=sk-...
```

## 已演示的 Deep Agents 能力

1. **主从调度**：Supervisor + `task` 委派多个 SubAgent
2. **自定义 Middleware**：日志、计时、审计（种子内置，Agent 勾选）
3. **Filesystem Backend**：按用户隔离的 `workspace/users/<scope>/` 沙箱
4. **Permissions**：路径级读写控制（`factory.build_permissions`）
5. **Memory / Skills**：项目级 `AGENTS.md` + 数据库 Skills（组装时物化）
6. **Checkpointer**：Redis Stack 多轮 `thread_id` 隔离（按用户前缀）
7. **HITL**：危险工具前暂停（`ENABLE_HITL=true`；当前主要挂在 Supervisor）
8. **方法论驱动**：DB 配置 → Agent Factory → 版本缓存；支持 MCP 工具

## 目录结构

```
DeepAgents/
├── main.py                 # CLI 入口（方法论组装）
├── server.py               # FastAPI 入口
├── alembic.ini             # Alembic 配置（script_location → migrations/）
├── migrations/             # Schema 迁移脚本
├── docker-compose.yml      # PostgreSQL + Redis Stack
├── deepagents_app/
│   ├── api/                # FastAPI 路由与 schemas
│   ├── auth.py             # Bearer introspect / 本地 AUTH_DISABLED
│   ├── db/                 # ORM / session / seed / bootstrap_session
│   ├── services/           # 业务服务与 Agent Factory
│   ├── registries/         # Tool（builtin/MCP）/ Middleware 加载
│   ├── workspace.py        # 用户工作区 ContextVar
│   ├── llm.py              # Chat Model 工厂
│   ├── factory.py          # checkpointer / permissions / GP 子 Agent
│   ├── utils/              # 路径安全 / 文本归一化
│   ├── tools/              # 内置工具实现
│   ├── middleware/
│   └── prompts/            # 种子子 Agent 系统提示（bootstrap 读入）
└── workspace/              # 运行时沙箱（users/<scope>/…）
```

## 说明

本仓库含教学与脚手架性质配置。生产环境请加强密钥脱敏、审计与 HITL 策略，并按需收紧 CORS。
