# DeepAgents 方法论平台（后端）

基于 [LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) 的**可配置方法论驱动多 Agent** 后端。

能力概览：

- FastAPI 配置与会话 API（同步聊天 + SSE）
- PostgreSQL：方法论 / Agent / Tool / Skill / Middleware / 模型目录（按用户隔离）
- Redis Stack：LangGraph checkpoint、Agent 缓存跨 worker 失效、多 worker 下 SSE 全局限流
- 按方法论动态 `create_deep_agent()` + 进程内 LRU；多进程经 Redis pub/sub 失效
- 方法论版本快照（旧会话锁定创建时版本）
- Skills 入库，组装时按内容指纹物化到 `workspace/users/<scope>/skills/`

前端：[`../DeepAgents-frontend`](../DeepAgents-frontend)

---

## 快速启动（本地推荐）

```bash
cd Agents-Project/DeepAgents

# 依赖（Python 3.13；以 pyproject.toml / uv.lock 为准）
uv sync --group dev
source .venv/bin/activate

cp .env.example .env
# 至少填写：OPENAI_API_KEY / OPENAI_BASE_URL（或 Anthropic）
# 本地可保持 AUTH_DISABLED=true、SECRETS_ALLOW_INSECURE_DEV_KEY=true

# 1) PostgreSQL + Redis Stack
docker compose up -d

# 2) Schema 迁移（API 启动不会自动 migrate）
python -m deepagents_app.db.migrate

# 3) 启动 API（默认 http://0.0.0.0:8001）
python server.py
```

另开终端起前端：

```bash
cd Agents-Project/DeepAgents-frontend
npm install
npm run dev
# http://localhost:5173
```

首次进入前端布局会调 `POST /api/bootstrap`，按当前用户幂等写入默认 Tool / demo 方法论。也可手动：

```bash
curl -X POST http://127.0.0.1:8001/api/bootstrap \
  -H "Authorization: Bearer <token>"   # AUTH_DISABLED=true 时可省略
```

API 文档：http://localhost:8001/docs · 探活：`GET /health`

---

## 进程模型（uvicorn / gunicorn）

入口统一为 `python server.py`，由 `.env` 控制：

| 变量 | 含义 |
|------|------|
| `API_HOST` / `API_PORT` | 监听地址（默认 `0.0.0.0:8001`） |
| `API_WORKERS` | 进程数；`1` 单进程，`>1` 多 worker |
| `API_SERVER` | `uvicorn` 或 `gunicorn`（后者使用 `UvicornWorker`） |
| `CHAT_STREAM_LIMITER` | `auto`：多 worker 时 SSE 用 Redis 全局限流；可强制 `local` / `redis` |

示例：

```env
# 本地调试（单进程）
API_WORKERS=1
API_SERVER=uvicorn

# 多 worker（生产/压测）
API_WORKERS=4
API_SERVER=gunicorn
```

等价手写：

```bash
uvicorn deepagents_app.api.app:app --host 0.0.0.0 --port 8001 --workers 4
gunicorn deepagents_app.api.app:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8001
```

多 worker 依赖可用的 Redis：缓存失效 pub/sub +（默认）SSE 全局槽位。Dockerfile 默认 `API_WORKERS=2`、`API_SERVER=gunicorn`。

---

## 鉴权与密钥

- 生产：`AUTH_DISABLED=false`，配置 `AUTH_INTROSPECT_URL`；请求头 `Authorization: Bearer <token>`，服务端解析 `user_id`，配置与会话按用户隔离。
- 本地：`AUTH_DISABLED=true` 时固定 `AUTH_DEV_USER_ID`。
- 模型 `api_key` 入库加密：生产设置 `SECRETS_ENCRYPTION_KEY`；仅本地可 `SECRETS_ALLOW_INSECURE_DEV_KEY=true`。

兼容 OpenAI 兼容接口（如 DeepSeek）：

```env
MODEL_PROVIDER=openai_compatible
OPENAI_BASE_URL=https://api.deepseek.com/v1
MODEL_NAME=deepseek-chat
OPENAI_API_KEY=sk-...
```

说明：`.env` 中的模型项主要用于 **bootstrap 种子默认模型**；已灌库后改 `.env` 不会自动改库里的模型行。

---

## Schema 迁移

单一基线迁移在 `migrations/versions/`。变更请用 Alembic（`alembic.ini` → `migrations/`；勿在仓库根再建 `alembic/` 目录，会遮蔽 PyPI 包）：

```bash
alembic revision --autogenerate -m "your change"
python -m deepagents_app.db.migrate
```

空库或换 volume 后：先 `migrate`，再 `bootstrap`。

---

## 主要接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/bootstrap` | 按用户幂等灌种子 |
| POST | `/api/methodology` | 创建方法论 |
| GET | `/api/methodology/list` | 列表（分页头 `X-Total-Count` / `X-Next-Cursor`） |
| POST | `/api/methodology/{id}/publish` | 发布 |
| POST | `/api/agent` | 创建 Agent |
| GET | `/api/tool/list` | 工具注册表 |
| GET | `/api/middleware/list` | 中间件注册表 |
| POST | `/api/conversation` | 创建会话（绑定方法论版本） |
| GET | `/api/conversation/{thread_id}/messages` | 历史消息 |
| POST | `/api/chat` | 聊天（同步 JSON） |
| POST | `/api/chat/stream` | 聊天（SSE） |
| POST | `/api/chat/resume` | HITL 恢复 |

流式并发受 `CHAT_STREAM_MAX_CONCURRENT` 限制；抢不到槽返回 **429**。

---

## 测试

需本机 Redis（`docker compose up -d`）。测试使用用户 `test-user` / `svc-test-user`，fixture 会清理其 checkpoint，且聊天 e2e 使用唯一 `thread_id`，避免污染开发会话。

```bash
uv sync --group dev
python -m pytest tests/ -q
```

---

## 运行时维护

Skills / content_blob 物化与快照正文按内容寻址，可手动或后台 GC：

```bash
python -m deepagents_app.services.skills_gc
python -m deepagents_app.services.skills_gc --max-age-days 7
python -m deepagents_app.services.content_blobs_gc
```

`SKILLS_GC_INTERVAL_HOURS` / `CONTENT_BLOB_GC_INTERVAL_HOURS`（默认 24）控制 API 进程内后台任务；`0` 表示仅手动。

两个间隔都是**全集群**语义。调度器在每个 worker 都会启动，但每轮执行前先用 `SET NX EX` 抢一把 Redis 锁（key 为 `deepagents:gc:skills` / `deepagents:gc:content_blob`，TTL 为间隔的 0.9 倍），同一窗口内只有一个进程真正执行清理，因此 `API_WORKERS` 调大不会让 GC 频率跟着放大。Redis 不可用时本轮直接跳过并记警告。

---

## 已演示的 Deep Agents 能力

1. **主从调度**：Supervisor + `task` 委派 SubAgent（种子：`qa-expert`）
2. **自定义 Middleware**：日志、计时、审计（种子内置，Agent 勾选）
3. **Filesystem Backend**：按用户隔离的 `workspace/users/<scope>/`
4. **Permissions**：路径级读写控制
5. **Memory / Skills**：`AGENTS.md` + 数据库 Skills（内容指纹物化）
6. **Checkpointer**：Redis Stack，按用户前缀隔离 `thread_id`
7. **HITL**：`ENABLE_HITL=true`；工具可单独 `requires_hitl`
8. **方法论驱动**：DB → Agent Factory → 版本缓存；支持 MCP 工具

---

## 目录结构

```
DeepAgents/
├── server.py               # 入口（uvicorn / gunicorn + UvicornWorker）
├── alembic.ini
├── migrations/             # Schema 迁移（单一 initial 基线）
├── docker-compose.yml      # PostgreSQL + Redis Stack（named volume）
├── Dockerfile              # 默认 gunicorn 多 worker；依赖以 uv.lock 为准
├── pyproject.toml / uv.lock
├── deepagents_app/
│   ├── api/                # 路由与 schemas
│   ├── auth.py
│   ├── db/                 # ORM / session / seed / migrate
│   ├── services/           # 业务、Agent Factory、cache pub/sub、GC
│   ├── registries/         # Tool / Middleware 加载
│   ├── tools/ · middleware/ · prompts/
│   └── factory.py          # checkpointer / permissions / GP 子 Agent
└── workspace/              # 运行时沙箱
```

---

## 说明

本仓库含教学与脚手架配置。生产请关闭 `AUTH_DISABLED`、配置真实 introspect 与 `SECRETS_ENCRYPTION_KEY`，收紧 CORS / MCP stdio，并按负载设置 `API_WORKERS` 与 DB 连接池。
