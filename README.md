# DeepAgents 方法论平台（后端）

基于 [LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) 的**可配置方法论驱动多 Agent** 后端。

能力概览：

- FastAPI 配置与会话 API（SSE 聊天）
- PostgreSQL：方法论 / Agent / Tool / Skill / Middleware / 模型目录（按用户隔离）
- Redis Stack：LangGraph checkpoint、Agent / MCP 缓存跨 worker 失效、多 worker 下 SSE 全局限流
- 按方法论动态 `create_deep_agent()` + 进程内 LRU；多进程经 Redis pub/sub 失效
- 方法论版本快照（旧会话锁定创建时版本）
- Skills 入库（SKILL.md 或目录包 zip），组装时按内容指纹物化到 `workspace/users/<scope>/skills/`
- 主 Agent 启用即发布同名方法论；启用后锁定编辑

前端：[`../DeepAgents-frontend`](../DeepAgents-frontend)

---

## 快速启动（本地推荐）

```bash
cd DeepAgents

# 依赖（Python 3.13；以 pyproject.toml / uv.lock 为准）
uv sync --group dev
source .venv/bin/activate

cp .env.example .env
# 至少填写：OPENAI_API_KEY / OPENAI_BASE_URL（或 Anthropic）
# 本地建议：AUTH_DISABLED=true、SECRETS_ALLOW_INSECURE_DEV_KEY=true
# （.env.example 里后者默认为 false，本地需改成 true）
# 本地调试可再设 API_WORKERS=1、API_SERVER=uvicorn（example 默认 2 + gunicorn）

# 1) PostgreSQL + Redis Stack
docker compose up -d

# 2) Schema 迁移（API 启动不会自动 migrate）
python -m deepagents_app.db.migrate

# 3) 启动 API（默认 http://0.0.0.0:8001）
python server.py
```

另开终端起前端：

```bash
cd ../DeepAgents-frontend
npm install
npm run dev
# http://localhost:5173
```

首次进入前端布局会调 `POST /api/bootstrap`，按当前用户幂等写入默认模型 / Tool / Middleware / Skills / demo Agents / 方法论。也可手动：

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

## 清除本地数据

没有单独的清库 CLI，本地开发可用下面两种方式。

### 1. 只清业务表（保留 schema）

```bash
docker exec -i deepagents-postgres psql -U deepagents -d deepagents -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
TRUNCATE TABLE conversation, methodology_revision, content_blob,
  methodology_agent, agent_tool, agent_middleware, agent_skill,
  agent_definition, methodology, tool_definition, skill_definition,
  middleware_definition, model_definition
  RESTART IDENTITY CASCADE;
COMMIT;
SQL
```

清完后重新 bootstrap（前端进布局一般会自动调，或手动）：

```bash
curl -X POST http://127.0.0.1:8001/api/bootstrap
```

此方式**不**清 Redis checkpoint；旧会话 thread 的图状态可能仍在。需要一并清时可对 Redis 执行 `FLUSHDB`，或改用下面的 volume 重建。

### 2. 整库连 volume 一起重来

```bash
docker compose down -v
docker compose up -d
python -m deepagents_app.db.migrate
# 再启动 API，然后 bootstrap
```

- `down`：停掉并删除当前 compose 启动的容器与网络。
- `-v`：同时删除 compose 声明的 named volume（本项目含 `deepagents_pg_data`、`deepagents_redis_data`、`deepagents_workspace`）。

对比：`docker compose down` 只停容器，**数据仍在 volume**；加 `-v` 才会把持久化数据一并删掉。

本地 `python server.py` 时 workspace 在项目目录 `./workspace`，**不受** compose volume 删除影响；若也要清空沙箱文件需自行删目录。

---

## 主要接口

下列为常用入口；完整 CRUD / 模型 / Skill 等见 http://localhost:8001/docs。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/bootstrap` | 按用户幂等灌种子 |
| POST | `/api/methodology` | 创建方法论 |
| GET | `/api/methodology/list` | 列表（分页头 `X-Total-Count` / `X-Next-Cursor`） |
| POST | `/api/methodology/{id}/publish` | 发布 |
| POST | `/api/agent` | 创建 Agent |
| GET | `/api/tool/list` | 工具注册表 |
| GET | `/api/middleware/list` | 中间件注册表 |
| GET | `/api/model/list` | 模型目录 |
| GET | `/api/skill/list` | Skill 目录 |
| POST | `/api/skill` | 用 SKILL.md 正文创建 Skill |
| POST | `/api/skill/upload` | 上传技能目录 zip（SKILL.md + 附属文件） |
| POST | `/api/conversation` | 创建会话（绑定方法论版本） |
| GET | `/api/conversation/{thread_id}/messages` | 历史消息 |
| POST | `/api/chat/stream` | 聊天（SSE） |
| POST | `/api/chat/resume/stream` | HITL 恢复（SSE） |

流式并发受 `CHAT_STREAM_MAX_CONCURRENT` 限制；抢不到槽返回 **429**。

---

## 测试

需本机 Redis（`docker compose up -d` 即可；测试库用 **SQLite**，不依赖 compose 里的 Postgres）。测试用户为 `test-user` / `svc-test-user`，fixture 会清理其 checkpoint，聊天 e2e 使用唯一 `thread_id`，避免污染开发会话。

```bash
uv sync --group dev
python -m pytest tests/ -q
```

---

## 运行时维护

Skills / content_blob 物化与快照正文按内容寻址，统一经 `services.infra.gc` 手动或后台清理：

```bash
python -m deepagents_app.services.infra.gc
python -m deepagents_app.services.infra.gc skills --max-age-days 7
python -m deepagents_app.services.infra.gc blobs
```

`SKILLS_GC_INTERVAL_HOURS` / `CONTENT_BLOB_GC_INTERVAL_HOURS`（默认 24）控制 API 内**同一个**后台 PeriodicTask；某项为 `0` 则后台跳过该项（仍可 CLI 手动）。

间隔是**全集群**语义：每个 worker 都会挂调度器，但 Skills / blob 各用一把 Redis 锁（`deepagents:gc:skills` / `deepagents:gc:content_blob`，TTL 为该项间隔的 0.9 倍），同一窗口内只有一个进程真正执行。Redis 不可用时本轮跳过并记警告。

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
│   ├── services/           # 业务分层：catalog / runtime / versioning / infra
│   │   ├── catalog/        # 方法论、Agent、Tool、Skill 等目录 CRUD
│   │   ├── runtime/        # Agent Factory、会话、聊天、流式限流
│   │   ├── versioning/     # 快照、升版、content_blob、Memory
│   │   └── infra/          # Redis、cache pub/sub、GC
│   ├── registries/         # Tool / Middleware 加载
│   ├── tools/ · middleware/ · prompts/ · skills/   # 内置工具与种子资源
│   ├── supervisor/         # Supervisor 系统提示
│   └── factory.py          # checkpointer / permissions / GP 子 Agent
└── workspace/              # 本地运行时沙箱（非 compose volume）
```

---

## 说明

本仓库含教学与脚手架配置。生产请关闭 `AUTH_DISABLED`、配置真实 introspect 与 `SECRETS_ENCRYPTION_KEY`，收紧 CORS / MCP stdio，并按负载设置 `API_WORKERS` 与 DB 连接池。
