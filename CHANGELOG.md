# 更新记录

## 2026-09-11 开发源码快照

- 补齐 Agent Core V1、意图识别 V3、书籍记忆 V1 和记忆—上下文—完成写回最小接线。
- 增加本地 PostgreSQL/MinIO 持久化预览边界：书籍内容原子写入、Job/Answer 事件、持久会话、Worker claim 和进度冲突控制。
- 保留开发预览边界：Gate 05/06 未正式通过，规范化读路径、独立 Worker、跨进程恢复和完整多端同步仍待后续。
- 公开说明统一以 `README.md`、本文件、`.env.example` 和 `LICENSE` 为准；内部治理与证据文件不作为公开源码内容。

## 0.0.6-stage05-conversation-preview

首次公开源码预览。包含无需划线的连续对话、可选原文引用、四路确定性路由，以及同书同版本的有限连续会话历史。

## Git 之前的历史里程碑

以下版本是项目治理文档记录的开发里程碑；在 `v0.0.6-stage05-conversation-preview` 之前，项目没有对应的 Git 标签或可追溯提交。

- `0.0.5-stage05-production-preview`：本地 PostgreSQL/MinIO 持久化边界与开发预览。
- `0.0.4-eval-preview`：确定性评测、真实 pgvector 试验与 Stage 04 契约复核。
- `0.0.3-persistence-preview`：SQLite 本地重启恢复预览。
- `0.0.2-stage05-preview`：上传、阅读、进度、划线和证据问答纵向切片。
- `0.0.1-design`：产品定义、架构草案、风险与阶段路线。
