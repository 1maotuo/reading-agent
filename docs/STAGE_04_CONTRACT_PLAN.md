# Stage 04 最小契约包规划

状态：规划草案；用一个实现任务冻结“登录会话 → 上传 → 任务 → Book/Chapter/Block/Chunk → 阅读 → 进度/划线 → 提问 → 工具 → SSE证据回答 → Trace”。Gate 03仍因F-03-02真实PostgreSQL/pgvector证据缺失而不通过；只允许无数据库依赖草案，不代表Stage/Gate 04通过。
## 1. 冻结原则

1. FastAPI模块化单体；REST处理命令/查询，SSE只承载回答事件；PostgreSQL为唯一业务库，任务表加独立Worker，不引入Redis/Celery。
2. 单主Agent、自研有限Runtime；模型只生成工具意图，权限、Scope、预算和证据门禁由服务端执行。
3. 客户端ID均是不可信目标；可信身份/授权来自HttpOnly Session和服务端授权记录；书内提问固定要求Evidence。
4. 失败、取消、证据不足和未验证数据库能力显式呈现；冻结后一次实现自测、最多一次独立复验；P0/P1阻断，P2待办。

## 2. 最小文件树

若项目已有同职责目录，合并到既有模块，不建立平行架构。

```text
src/reading_agent/
  contracts.py        # Pydantic v2模型、枚举、错误、SSE及工具公共Schema
  domain.py           # 跨模型不变量、状态机、证据门禁和原子发布规则
  ports.py            # Session/Repository/ObjectStore/Model/Tool/Event端口Protocol
  api.py              # FastAPI路由与依赖注入；只调用公开domain/port接口
  worker.py           # 任务租约、检查点、有限重试、取消和发布编排
db/migrations/
  0001_stage04_contract_draft.sql  # 仅审查草案，不执行、不声称pgvector通过
tests/contracts/
  test_models.py
  test_http_sse.py
  test_jobs_tools.py
```

公开依赖方向固定为：`api/worker → contracts + domain + ports`；`domain → contracts + ports`；`contracts` 不读取任何实现模块。模块调用者不需要读取对方内部实现。
## 3. Pydantic v2 核心合同

公共基类：`ConfigDict(extra='forbid', strict=True)`；所有时间为 UTC aware datetime；实体 ID 为 UUID；游标为服务端签发的不透明字符串。

### 枚举

- `BookFormat`: `pdf | epub | txt | markdown`
- `BookVersionStatus`: `building | ready | failed | deleting | deleted`
- `JobType`: `import_book | delete_book`
- `JobStatus`: `queued | running | retry_wait | succeeded | failed | cancel_requested | cancelled`
- `JobStage`: `validate | stage_object | parse | build_blocks | build_chunks | embed | verify | publish | purge`
- `AnswerRunStatus`: `accepted | running | completed | failed | cancelled`
- `ToolName`: 六个固定工具名
- `ToolCallStatus`: `started | succeeded | failed | disabled`
- `SSEEventType`: `accepted | tool_started | tool_finished | evidence | answer_delta | completed | failed | heartbeat`
- `ErrorCode`: 见第 8 节固定集合

### 服务端 Scope

`ScopeContext` 使用 `ConfigDict(frozen=True, extra='forbid', strict=True)`；字段为 `session_id, user_id, book_id, book_version_id, chapter_id?, furthest_chunk_index?, request_id, trace_id`。

Scope 只能由认证依赖读取 Session，再查询用户对书籍/版本的授权记录后构造；禁止出现在客户端请求模型、工具参数和模型输出中。任何目标 ID 与授权不一致统一拒绝，不采用客户端值修补 Scope。

### 实体与请求模型

- 身份/书：`SessionView(session_id,user_id,expires_at)`；`Book(book_id,user_id,title,format,active_version_id?,status,created_at,row_version)`；`BookVersion(...file_sha256,pipeline_version,status,published_at?)`。
- 正文：`Chapter(book_version_id,ordinal,title,source_locator)`；`Block(chapter_id,ordinal,text,text_sha256,source_locator)`；`Chunk(chapter_id,chunk_index,block_ids,text_sha256,token_count,embedding_model,chunker_version)`。
- 阅读：`ReadingPosition(chapter_id,block_id,block_offset,updated_at,device_id,row_version)`；`HighlightAnchor(book_version_id,chapter_id,start/end,exact_quote,prefix,suffix,text_sha256,orphaned)`。
- 回答：`EvidenceRef(完整复合身份,chunk_id,block_ids,quote,content_sha256,source_locator)`；非空去重`EvidenceBundle`；`QuestionCreate(question,highlight_id?,client_request_id)`不得含Scope/request_type；`AnswerRunView`与脱敏`TraceView`。

### 跨模型不变量

1. BookVersion/Chapter/Block/Chunk/Evidence/Highlight绑定同一 `user_id+book_id+book_version_id`；章节/Block不得跨版本。
2. ordinal/chunk_index非负、同父级唯一稳定；Chunk不跨章节，Block列表非空有序，hash来自可信正文。
3. Evidence由服务端回读并校验复合身份、正文hash、Block顺序和进度；失败不得进入模型/SSE。
4. Highlight端点属于正文Block、offset合法有序；quote/hash不一致则orphan；last可回退、furthest单调、row_version乐观锁。
5. parse/chunk/embed/verify全部成功后才在单事务切换active_version；失败版本不可查询。
6. 首个answer_delta前必须有Evidence；SSE seq单调、唯一终态、终态后隔离迟到事件。
7. 删除中禁止读取/提问；完成后正文、向量、对象和缓存不可访问。

## 4. REST 最小契约

统一前缀 `/api/v1`；JSON 使用上述模型；修改请求支持 `Idempotency-Key`；错误统一为第 8 节 envelope。

| 方法与路径 | 输入 | 成功输出 | 关键约束 |
|---|---|---|---|
| `POST /sessions` | identifier/password | `200 SessionView` + HttpOnly Cookie | 登录失败不区分账号是否存在 |
| `GET /session` | Cookie | `200 SessionView` | 无会话 `401` |
| `DELETE /session` | Cookie/CSRF | `204` | 服务端立即失效 |
| `POST /books` | multipart file/title + Idempotency-Key | `202 {book_id,job_id}` | 格式/大小/hash校验，先私有暂存 |
| `GET /books/{book_id}` | path | `200 Book` | owner-only；跨用户返回不枚举的 `404` |
| `DELETE /books/{book_id}` | If-Match/CSRF | `202 {job_id}` | 立即 tombstone，异步清理 |
| `GET /jobs/{job_id}` | path | `200 JobView` | owner-only |
| `POST /jobs/{job_id}/retry` | Idempotency-Key | `202 JobView` | 只允许可重试失败且未超上限 |
| `POST /jobs/{job_id}/cancel` | Idempotency-Key | `202 JobView` | 终态幂等；publish提交后不可回滚 |
| `GET /books/{book_id}/chapters` | cursor/limit | `200 Page[Chapter]` | 只读 active ready version |
| `GET /books/{book_id}/chapters/{chapter_id}/blocks` | cursor/limit | `200 Page[Block]` | chapter必须属于Scope |
| `GET /books/{book_id}/progress` | path | `200 ReadingProgress` | owner-only |
| `PUT /books/{book_id}/progress` | ReadingPosition + If-Match | `200 ReadingProgress` | 更新last；furthest单调；冲突`409` |
| `GET /books/{book_id}/highlights` | cursor/limit | `200 Page[HighlightAnchor]` | active/历史版本均显式标识 |
| `POST /books/{book_id}/highlights` | HighlightCreate + Idempotency-Key | `201 HighlightAnchor` | 服务端复核Block/quote/hash |
| `DELETE /books/{book_id}/highlights/{id}` | CSRF | `204` | owner/book双重约束 |
| `POST /books/{book_id}/questions` | QuestionCreate + Idempotency-Key | `202 AnswerRunView` | 创建Trace；书内证据门禁固定开启 |
| `GET /answer-runs/{run_id}/events` | `Last-Event-ID?` | `text/event-stream` | owner-only；按seq续传；终态可重放 |
| `GET /traces/{trace_id}` | path | `200 TraceView` | owner-only且脱敏 |

## 5. SSE 最小契约

每帧：`id: <seq>`、`event: <SSEEventType>`、`data: <SSEEnvelope JSON>`。Envelope 固定含 `run_id, trace_id, seq, emitted_at, type, payload`。

- `accepted`只发一次；`tool_started/tool_finished`仅含call/name/status/result_ref/error_code，不回显完整参数。
- `evidence`为受控引用/短quote且先于正文；`answer_delta`只含text_delta，不含Scope/Prompt/原始工具响应。
- `completed(answer_id,conversation_id,evidence_ids,usage)`或`failed(error.code,retryable)`为唯一终态，不附堆栈。
- `heartbeat`无业务状态变化，不递增业务版本。

断连只停止向该连接写入，不允许迟到任务越过 run 终态；同一 `Last-Event-ID` 重连不得重复产生工具调用。
## 6. 六个工具 Schema

所有输入均 `extra='forbid'`，不含 `user_id/book_id/version/chapter/Scope`；Runtime 注入不可变 Scope。统一输出 `ToolResult {call_id,name,status,data?,evidence_refs[],error?}`。

| 工具 | 输入 | 输出 | 限制 |
|---|---|---|---|
| `search_book` | `query, top_k=1..10, chapter_ids?` | 排序后的 `EvidenceCandidate[]` | keyword/vector/RRF/rerank均经Scope与进度过滤；真实pgvector效果blocked |
| `read_book_blocks` | `block_ids[1..20], context_before/after=0..2` | `BlockExcerpt[]` | 服务端按可信顺序回读并验hash |
| `get_book_structure` | `chapter_cursor?, limit=1..100` | `ChapterSummary[]` | 只读active ready版本 |
| `search_reading_memory` | `query, kinds?, limit=1..10` | `MemoryHit[]` | 仅当前用户/书；不返回隐式心理评分 |
| `web_search` | `query, allowed_domains?, limit=1..5` | `WebSearchHit[]` | `disabled_by_default`；未启用固定返回`tool_disabled`，不联网 |
| `read_web_source` | `source_id` | `WebSourceExcerpt` | `disabled_by_default`；只接受前序白名单结果的opaque ID，不接受任意URL |

本工作包不得实现或启用外部 MCP；两个 Web 工具只有可验证的禁用合同。
## 7. 任务、重试、取消与原子发布

- 主迁移：`queued→running→succeeded|failed|cancel_requested`；`failed(retryable)→retry_wait→queued`；`cancel_requested→cancelled`。
- 领取写lease owner/expiry/heartbeat，过期才可接管；`idempotency_key+user_id+operation`唯一，重复返回原job/run。
- attempt从1开始最多3次；仅临时依赖错误重试；Schema/权限/格式/Evidence错误不可重试。
- 每阶段保存stage/checkpoint/input_hash/pipeline_version，恢复前验hash；取消在阶段边界协作执行。
- 新版本写building隔离区；verify检查数量/身份/hash；publish单事务切active并置ready，失败保留旧ready版本。

## 8. 错误、权限、删除与脱敏

错误 envelope：`{error:{code,message,request_id,retryable,details?}}`；`details` 只能是字段名、限制值和不敏感实体 ID。

固定错误码：`unauthenticated(401)`、`csrf_failed(403)`、`not_found(404)`、`forbidden(403，仅非枚举场景)`、`conflict(409)`、`version_conflict(409)`、`idempotency_conflict(409)`、`invalid_input(422)`、`unsupported_format(415)`、`payload_too_large(413)`、`book_not_ready(409)`、`invalid_job_state(409)`、`anchor_invalid(422)`、`tool_disabled(409)`、`tool_timeout(504)`、`evidence_required(422/SSE failed)`、`rate_limited(429)`、`internal_error(500)`。
- Cookie为HttpOnly/Secure/SameSite=Lax、登录后轮换；修改端点校验CSRF。Repository方法必须接收Scope，SQL范围条件不可省略。
- 跨用户访问统一404；工具/模型不得扩大Scope。删除先tombstone并撤权，再取消未提交任务并清对象/正文/Chunk/向量/划线/会话/缓存。
- 备份按保留期过期；Trace只留删除ID/hash/时间。日志只允许request/trace/run/job ID、伪名user、状态/耗时/计数/版本/错误码。
- 禁记密码、Cookie/Token、密钥、签名URL、完整文件/书文/问答/Evidence、原始工具响应和含敏感参数堆栈。

## 9. PostgreSQL DDL 审查草案

`0001_stage04_contract_draft.sql` 只定义并接受静态审查，不在本工作包执行：

- 表：`users,sessions,books,book_versions,chapters,blocks,chunks,jobs,reading_progress,highlights,conversations,conversation_turns,answer_runs,answer_events,traces,idempotency_keys`。
- 书内表显式含user/book/version并用复合UNIQUE/FK防跨范围；ordinal/chunk_index非负且同父级唯一；Chunk Block列表非空。
- progress含row_version且last/furthest分列；jobs含状态/阶段CHECK、attempt、lease、取消时间、checkpoint和唯一幂等键。
- answer_events的run_id+seq唯一；active_version只指向同user/book ready版本；循环FK用延迟约束或分步迁移。
- 删除用tombstone+受控级联，对象删除由job确认。
- `embedding vector(1024)`仅为可空草案；extension、索引、10k、p95、EXPLAIN均标`BLOCKED_BY_F03_02`，禁止模拟通过。

## 10. 冻结测试矩阵

| ID | 合同测试 | 必须断言 |
|---|---|---|
| C01 | 严格模型 | extra/错型/naive时间拒绝；Scope frozen |
| C02 | Scope来源 | 客户端/header/body/model伪造身份均不能成为可信Scope |
| C03 | 跨模型身份 | Chapter/Block/Chunk/Evidence/Highlight任一跨范围即失败 |
| C04 | Session | 成功、失败不枚举、过期、注销、CSRF |
| C05 | 上传幂等 | 同key同hash返回原job；同key异hash冲突；格式/大小失败 |
| C06 | Job状态机 | 合法迁移、非法迁移、租约接管、最多3次、取消幂等 |
| C07 | 原子发布 | verify失败不切active；成功单事务切换；旧版本持续可读 |
| C08 | 阅读查询 | 只读ready active版本；章节归属/cursor/limit校验 |
| C09 | 进度并发 | last可回退、furthest单调、If-Match冲突不覆盖 |
| C10 | 划线 | 合法端点；负/越界offset、错quote/hash、跨版本拒绝或orphan |
| C11 | 四个本地工具 | 参数严格、Scope注入、越界结果拒绝、Evidence hash回读 |
| C12 | 两个Web工具 | 默认始终`tool_disabled`；不得产生网络调用 |
| C13 | Evidence门禁 | 无证据、错范围、错版本、错hash均不得发answer_delta/completed |
| C14 | SSE状态机 | seq单调、事件顺序、唯一终态、终态后关闭、重连不重复工具 |
| C15 | 错误与脱敏 | HTTP/SSE错误码稳定；日志/Trace不含禁记字段 |
| C16 | 删除 | tombstone立即拒读；任务取消；级联/对象/备份状态可追踪 |
| C17 | OpenAPI边界 | 所有端点绑定请求/响应模型；工具/Scope内部字段不暴露 |
| C18 | DDL静态审查 | 表/约束/FK/索引占位与模型一致；pgvector执行明确blocked |

矩阵到实现开始即冻结；独立复验只运行 C01–C18。新发现的非核心优化列 P2，不在本轮扩展测试集。
## 11. Gate 04 通过标准

1. 9个最小文件完成且无平行定义；C01–C18全通过、真实外层0、无skipped/xfail冒充。
2. OpenAPI与六工具Schema两次确定性导出SHA一致；Scope不出现在客户端/模型Schema。
3. 失败路径fail-closed、无P0/P1；P2有负责人/阶段；DDL只可记“静态审查通过”，不得声称数据库执行。
4. 最小证据含范围、命令、退出码、18项数量、Schema/关键文件hash、限制和结论。
5. Luna一次自测，最多一次Sol复验；P0/P1修复只复跑失败项及C02/C03/C06/C07/C13/C14。
6. Gate03未通过时最多登记“Gate04条件已满足、阶段未正式完成”，不得绕过推进规则。

## 12. 本轮明确砍掉

- UI/PWA/PDF.js、对象存储真实接入、跨设备UI；PostgreSQL迁移执行、pgvector性能、真实模型/Embedding/Reranker。
- Redis/Celery/Kafka/Elasticsearch/Milvus/Kubernetes、微服务、LangChain/LangGraph、多Agent/长ReAct/微调/自动改Prompt。
- Web工具联网、任意URL、第三方MCP；微信/手机登录、语音、OCR、跨书图谱、多人共读。
- 生产部署/备份演练、完整前端E2E、真实大书、成本/容量/安全测试留Stage05–07。

## 13. 单一 Luna 实现任务顺序

任务名：`stage04_minimum_contract_package`；不得再拆多个实现任务。

1. 建最小树与单向依赖；在`contracts.py`一次实现模型、枚举、错误、SSE和工具Schema。
2. 在`domain.py/ports.py`实现Scope边界、跨模型不变量、Evidence门禁和公开端口。
3. 在`api.py`绑定REST/OpenAPI，不做UI或真实外部Provider。
4. 在`worker.py`实现状态机、租约、重试、取消、检查点和发布编排。
5. 写DDL静态草案并标明F-03-02 blocked项。
6. 仅按冻结矩阵写三份测试；执行一次C01–C18自测并生成最小证据。
7. 一个独立任务复核一次；无P0/P1即停止并准备Stage05纵向切片。
