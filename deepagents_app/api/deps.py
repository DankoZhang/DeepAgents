"""FastAPI 公共依赖：当前登录用户。"""

from __future__ import annotations

from deepagents_app.auth import get_current_user_id

# 鉴权：require_user 不再隐式灌种子；请调用 POST /api/bootstrap
require_user = get_current_user_id
