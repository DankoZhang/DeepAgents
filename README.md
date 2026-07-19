# DeepAgents 方法论平台（后端）

基于 [LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) 的**可配置方法论驱动多 Agent 平台**后端。

支持：

- FastAPI 配置与会话 API
- PostgreSQL 方法论 / Agent / Tool / Middleware 配置库
- Redis（LangGraph checkpoint）多轮会话隔离
- 按方法论动态 `create_deep_agent()` + 进程内缓存
- 方法论版本快照（旧会话锁定创建时版本）

前端仓库：[`../DeepAgents-frontend`](../DeepAgents-frontend)

---

## 平台模式（推荐）

```bash
cd Agents-Project/DeepAgents

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 API Key

# 启动 PostgreSQL + Redis
docker compose up -d

# 启动 FastAPI（默认 http://0.0.0.0:8000）
python server.py
```

另开终端启动前端：

```bash
cd Agents-Project/DeepAgents-frontend
npm install
npm run dev
# http://localhost:5173
```

API 文档：http://localhost:8000/docs

主要接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/methodology` | 创建方法论 |
| GET | `/api/methodology/list` | 列表 |
| POST | `/api/methodology/{id}/publish` | 发布 |
| POST | `/api/agent` | 创建 Agent |
| GET | `/api/tool/list` | 工具注册表 |
| GET | `/api/middleware/list` | 中间件注册表 |
| POST | `/api/conversation` | 创建会话（绑定方法论版本） |
| GET | `/api/conversation/{thread_id}/messages` | 历史消息 |
| POST | `/api/chat` | 聊天 |
| POST | `/api/chat/resume` | HITL 恢复 |

冒烟测试（不依赖 LLM）：

```bash
python -m pytest tests/test_api_mvp.py -q
```

---

## CLI 演示模式（YAML）

不走配置库时，可用 YAML 演示工厂：

```bash
python main.py
# 或按数据库方法论组装
python main.py --methodology demo_deepagents
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
2. **自定义 Middleware**：日志、计时、审计
3. **Filesystem Backend**：本地 `workspace/` 沙箱
4. **Permissions**：路径级读写控制
5. **Memory / Skills**：`AGENTS.md` + `SKILL.md`
6. **Checkpointer**：Redis 多轮 `thread_id` 隔离
7. **HITL**：危险工具前暂停（`ENABLE_HITL=true`）
8. **方法论驱动**：DB 配置 → Agent Factory → 版本缓存

## 目录结构

```
DeepAgents/
├── main.py                 # CLI 入口
├── server.py               # FastAPI 入口
├── docker-compose.yml      # PostgreSQL + Redis
├── deepagents_app/
│   ├── api/                # FastAPI 路由与 schemas
│   ├── db/                 # ORM / session / seed
│   ├── services/           # 方法论 / Agent Factory / 会话 / Chat
│   ├── registries/         # Tool / Middleware class_path 加载
│   ├── factory.py          # YAML 演示组装
│   ├── tools/
│   ├── middleware/
│   └── skills/
└── workspace/
```

## 说明

本仓库含教学与脚手架性质配置。生产环境请加强密钥脱敏、审计与 HITL 策略，并按需收紧 CORS。
