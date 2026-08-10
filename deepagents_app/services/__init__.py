"""
业务服务层
==========

按职责分子包，请直接从子包导入，例如::

    from deepagents_app.services.catalog import agents, tools
    from deepagents_app.services.runtime import agent_factory, chat
    from deepagents_app.services.versioning import revisions, content_blobs
    from deepagents_app.services.infra import gc, cache_pubsub

- ``catalog``     方法论 / Agent / Tool / Skill / Middleware / LLM 目录 CRUD
- ``runtime``     Agent 组装、会话、聊天流式
- ``versioning``  快照、升版、content_blob、Memory
- ``infra``       Redis、缓存失效广播、GC、周期任务
"""
