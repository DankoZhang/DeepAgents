"""
业务服务层
==========

路由一般 ``from deepagents_app.services import xxx as svc`` 引用子模块。

子模块职责一览：
- ``methodology``   方法论 CRUD / 发布 / 勾选 Agent
- ``revisions``      快照序列化、升版、缓存失效登记
- ``agents``         全局 Agent CRUD 与 Tool/Middleware/Skill 绑定
- ``tools`` / ``middlewares`` / ``skills`` / ``llm_models``  各类目录资源
- ``agent_factory``  按方法论（live 或快照）组装 Compiled Agent
- ``conversation``   会话创建（锁定 version）与查询
- ``chat``           invoke / resume / 读 checkpointer 历史
"""
