# 页伴｜AI 阅读助手

一个面向个人阅读的开发预览：上传 PDF、EPUB、TXT 或 Markdown，在网页中阅读、保存进度，并直接和书籍对话。划线是可选的原文引用，不是开始对话的前置条件。

当前是 2026-09-11 的开发源码快照，已经包含连续对话、意图识别、书籍记忆、上下文组装和本地 PostgreSQL/MinIO 持久化预览。项目仍在持续开发中，重点是提升理解效果、交互体验和陪伴感。

## Windows 快速启动（SQLite，无 Docker）

在项目根目录执行：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-stage03.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-stage05-production.txt
Set-Location web
pnpm install
pnpm run build
Set-Location ..
.\tools\start_stage05_preview.ps1
```

然后打开 `http://127.0.0.1:8765/`。默认开发账号是 `reader@example.local`，密码是 `reading-demo`。脚本默认使用本地 SQLite 和 filesystem，不需要 Docker、PostgreSQL 或 MinIO。

如需使用已经存在的本机 PostgreSQL/MinIO 预览服务，显式运行 `.\tools\start_stage05_preview.ps1 -UseLocalServices`；也可以先设置环境变量覆盖默认值。

## 环境变量

复制 `.env.example` 后按需设置环境变量。`DASHSCOPE_API_KEY` 只从环境读取，不能提交到仓库；没有模型凭证时使用明确标记的本地回退。设置 `READING_AGENT_PREVIEW_EMAIL` 和 `READING_AGENT_PREVIEW_PASSWORD` 可更换开发预览账号。

默认不设置 `READING_AGENT_STAGE05_POSTGRES_DSN`、`READING_AGENT_STAGE05_MINIO_*` 时使用 SQLite/filesystem。使用 `-UseLocalServices` 会补入本机预览服务默认值，显式环境变量优先。

## 当前边界

- 直接连续对话是主路径，划线只是可选的原文引用；回答围绕当前书籍内容和对话上下文展开。
- 书籍记忆支持有界召回、用户查看/清除和学习信号记录，后续继续提升它对用户理解状态的帮助。
- 本机 PostgreSQL/MinIO 可以用于开发预览；完整的生产读写链路、独立 Worker、跨进程恢复和多端同步仍在开发中。
- 真实模型质量、生产性能、云对象存储、正式认证和正式部署仍在开发中。

## 接下来

继续围绕三个方向迭代：让 AI 更理解用户，让用户更容易理解书籍，以及让连续对话更自然。具体功能在开发过程中逐步讨论和确定。

## 开源许可

本项目按 Apache License 2.0 发布，详见 `LICENSE`。运行前按需配置 `.env.example` 中的模型、数据库和对象存储变量。
