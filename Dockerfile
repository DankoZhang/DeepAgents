# DeepAgents API
# 构建：docker build -t deepagents-api .
# 运行前需可用的 Postgres + Redis；schema 请在部署时单独 migrate，勿依赖容器启动自动迁。
FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_SYSTEM_PYTHON=1 \
    PATH="/app/.venv/bin:$PATH" \
    API_HOST=0.0.0.0 \
    API_PORT=8001 \
    API_WORKERS=2 \
    API_SERVER=gunicorn

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

# 先拷依赖清单与包源，便于层缓存；锁文件必须与 pyproject 一致
COPY pyproject.toml uv.lock README.md ./
COPY deepagents_app ./deepagents_app
COPY migrations ./migrations
COPY alembic.ini server.py AGENTS.md ./

RUN uv sync --frozen --no-dev \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${API_PORT}/health" || exit 1

CMD ["python", "server.py"]
