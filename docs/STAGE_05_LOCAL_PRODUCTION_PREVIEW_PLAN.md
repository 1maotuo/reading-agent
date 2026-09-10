# Stage 05 本地生产预览计划

- 日期：`2026-09-09`
- 状态：本轮实现与有界验收完成；Gate 05 仍未正式通过。

## 本轮用户决策

- 对象存储先使用本机私有 MinIO，不接云 OSS/S3 账号。
- 认证先做邮箱/密码开发预览，不做微信登录。
- 多端冲突使用 `row_version` + `If-Match` 乐观锁；旧版本返回冲突，不静默覆盖。

## 实现边界

配置 `READING_AGENT_STAGE05_POSTGRES_DSN` 和 `READING_AGENT_STAGE05_MINIO_*` 后，Stage 05 工厂会幂等应用 `0002` 至 `0005` 迁移，使用 PostgreSQL 承接当前合同对象的重启状态，使用私有 MinIO 保存服务端生成键下的原书对象，并运行邮箱/密码预览、Evidence/SSE 和 `row_version` 冲突检查。

未配置时仍可用 SQLite/文件系统开发回退；配置 PostgreSQL 时如果 MinIO 不可用则直接失败，不静默回退。

## 暂不宣称

当前仍不是正式生产部署：规范化 Repository 尚未全量接管 API/Runtime，Job 仍在纵向切片请求内同步执行，账号/会话未持久化，跨进程多端原子同步、全新环境 E2E、真实模型质量、成本和云 OSS 尚未验收。
