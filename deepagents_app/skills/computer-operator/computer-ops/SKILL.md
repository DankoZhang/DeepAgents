---
name: computer-ops
description: >
  在受限 workspace 内进行文件与命令行操作的安全规范。
  当需要列目录、读写文件、跑白名单 shell 时使用本 skill。
---

# 计算机操作 Skill

## 安全原则

1. **最小权限**：只访问任务需要的路径
2. **先观察后修改**：`list` / `read` 成功后再 `write`
3. **命令白名单**：只使用允许的命令前缀
4. **失败可见**：如实回报 exit_code 与 stderr

## 推荐操作顺序

```
list_workspace → read_workspace_file →（确认）→ write / run_shell
```

## 禁止事项

- 尝试访问 workspace 外路径
- `rm` / `sudo` / 管道下载执行
- 在未授权时覆盖重要文件
