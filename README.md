# 页伴｜AI 阅读助手

一个面向个人阅读的开发预览：上传 PDF、EPUB、TXT 或 Markdown，在网页中阅读、保存进度，并直接和书籍对话。划线是可选的原文引用，不是开始对话的前置条件。

当前是 2026-09-11 的开发源码快照，包含 Agent Core V1、意图识别 V3、书籍记忆 V1 和本地 PostgreSQL/MinIO 持久化预览。这是开发预览，不是生产版本；Gate 05 与 Gate 06 尚未正式通过。

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

- 直接连续对话是主路径，划线只是可选的原文引用；回答会受书籍范围、证据门禁和有限上下文约束。
- 开发预览包含书籍记忆的有界召回、用户查看/清除和明确卡住后的候选写回；它不是完整的生产级长期记忆系统。
- 本机 PostgreSQL/MinIO 预览已覆盖部分持久化边界；规范化 Book/Answer 读路径、独立 Worker、跨进程恢复、完整多端同步和全新环境 E2E 仍未完成。
- 真实模型质量、提示注入防护、Token 成本、生产性能、云对象存储、正式认证和正式部署仍未完成。
- 不提交私有书籍、运行时数据、内部证据、实验生成物或本机 Docker 修复脚本。

阶段范围与验收口径见 `docs/PRODUCT_SPEC.md` 和 `docs/STAGE_05_VERTICAL_SLICE_PLAN.md`。

## 开源许可

本项目按 Apache License 2.0 发布，详见 `LICENSE`。提交前请自行配置 `.env.example` 中的模型、数据库和对象存储变量；真实密钥不得写入仓库。
