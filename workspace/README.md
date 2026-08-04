# DeepAgents Workspace

本目录是 Agent 的本地沙箱根目录（FilesystemBackend `root_dir`）。

- `documents/` — 文档撰写子 Agent 输出
- `notes/` — 问答笔记
- `audit/` — 敏感工具审计日志（禁止 Agent 改写）
- `AGENTS.md` — 由工厂从项目根同步
- `skills/<agent_id>/` — 按 Agent 绑定从数据库物化的 Skills

请勿把真实密钥放进本目录。
