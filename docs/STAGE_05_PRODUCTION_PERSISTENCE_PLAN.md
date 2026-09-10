# Stage 05 生产持久化边界工作包

- 日期：`2026-09-09`
- 状态：窄范围实现与自测完成；不代表 Gate 05 正式通过。

## 冻结范围

沿 Stage 04 已冻结的 `BookRepositoryPort` 与 `PublishTransactionPort`，补充一个真实 PostgreSQL/pgvector 可执行边界：

1. `db/migrations/0002_stage05_postgres_bootstrap.sql`：建立用户、书籍、版本、章节、Block、Chunk、Job、阅读进度和 `vector(1024)`，并建立范围、GIN、HNSW 与任务领取索引。
2. `src/reading_agent/postgres_adapter.py`：连接/事务生命周期、参数化的范围读取、签名游标、进度乐观锁和 verified-version 原子发布。
3. 使用仅绑定 `127.0.0.1:15432` 的隔离 pgvector 容器验证真实迁移和事务；不修改既有 Docker 容器、数据盘、镜像或卷。

## 明确不包含

生产认证/密码策略、对象存储、证据历史/问答持久化、多端冲突策略、独立 Worker 部署、前端切换和生产发布。这些仍是 Gate 05 后续硬门；SQLite 仍保留为本地开发适配器。

## 完成条件

- 迁移可在隔离 PostgreSQL/pgvector 中应用并重复应用。
- 书籍、章节、Block、Chunk 读取始终带用户/书籍/版本范围；书籍级 Scope 可读取指定 Chunk，但不能越过活动 ready 版本。
- 进度首次写入、乐观锁更新、旧版本冲突和 `furthest_chunk_index` 单调性得到真实数据库结果。
- 版本发布、active-version 切换和 Job 成功状态在同一事务中提交；异常回滚不留下半成品。
- Stage 04 合同和 Stage 05 既有开发预览回归继续通过。

## 停止边界

本工作包通过后只记录真实数据库适配边界，不把它升级为 Gate 05。下一次正式 Gate 05 还需在全新环境验证上传、对象存储、持久 Worker、问答/证据持久化、重启和多端同步；若需要外部账号、付费服务或产品决策则暂停对应分支。
