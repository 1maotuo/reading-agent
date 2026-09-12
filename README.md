# 页伴｜AI 阅读助手

页伴是一个面向个人阅读的 AI 助手：用户可以导入 PDF、EPUB、TXT 或 Markdown，在网页中阅读、保存进度，并围绕书籍进行连续对话。产品目标有两个：帮助用户真正理解一本书，以及在长期阅读过程中形成自然的陪伴感。

这份 README 同时是当前源码的**真实实现与接线说明**。阅读代码时请严格区分：

- 一个类、接口、数据库表或测试文件存在，不等于它已经进入默认产品链路。
- 默认检索仍为 BM25；配置后 DashScope embedding、本地/PG 向量检索与 RRF 已接入。
- “有记忆数据结构”不等于系统已经形成完整的长期用户理解。
- 本文状态来自对启动入口、API、运行时和持久化调用链的静态核对，不根据历史计划推断。

当前源码快照日期：2026-09-11。

## 一分钟结论

当前已经能运行一个完整的本地阅读演示：登录、导入书籍、阅读、保存进度、直接聊天、临时引用原文、流式显示回答、停止回答、查看引用、基础设置、浏览器语音输入与朗读都已经接入。

但以下四点必须特别注意：

1. **默认书籍检索仍然是本地 BM25；embedding 混合检索是显式 opt-in。** 设置 `READING_AGENT_ENABLE_EMBEDDINGS=1` 并提供 `DASHSCOPE_API_KEY` 后，导入会生成 1024 维向量并以 RRF 混排；失败时继续 BM25。
2. **语义意图识别和模型回答需要 `DASHSCOPE_API_KEY`。** 有密钥时使用轻量模型做意图与认知判断，再用回答模型生成内容；没有密钥时会退回确定性的关键词路由和本地提示文本。
3. **PostgreSQL＋MinIO 是部分接线，不是完整运行时切换。** 书籍内容写入、进度、认证、Job 和 Answer 的若干持久化路径已经接入；但书籍、章节、对话等主要 API 仍以进程内对象为当前读取层，并用整体快照恢复。源码明确报告 `runtime_cutover=false`。
4. **书籍学习记忆已经形成保守的基础闭环，但还没有达到完整的长期用户理解。** 明确困惑会按用户原话和原文建立懒加载概念锚点；后续的继续困惑、明确理解、复述或纠正可以确认候选信号并更新概念状态，下一轮会沿用概念改写模糊检索。跨书画像、自动遗忘和更强的复述正确性验证仍未实现。

## 状态标记

| 标记 | 含义 |
| --- | --- |
| ✅ 默认已接入 | 不增加外部配置，按默认启动方式即可进入实际产品路径 |
| 🟡 配置后已接入 | 代码已进入运行路径，但需要模型、PostgreSQL 或 MinIO 配置 |
| 🟠 部分实现/尚未完全接入 | 有可用代码、接口、表结构或局部调用，但默认主链路并未完整使用 |
| ⬜ 尚未实现 | 当前只有想法、枚举/合同，或源码中没有实际能力 |

## 当前默认运行链路

```text
React 网页（web/src/App.tsx）
  ↓ Cookie 会话 + CSRF
FastAPI（src/reading_agent/api.py）
  ↓
进程内服务对象 ApiServices
  ├─ 书籍、章节、Block、Chunk、进度、回答、记忆
  └─ 整体快照保存到 SQLite
  ↓
HybridIntentRouter
  ├─ 有 DASHSCOPE_API_KEY：轻量模型一次返回 IntentFrame + CognitiveFrame
  └─ 无密钥/模型异常：关键词规则降级
  ↓
ReaderAnswerHandler（固定单 Agent 流程）
  ├─ 普通聊天：模型回答；失败时使用本地提示
  └─ 书籍问题：LocalBookToolProvider 做词面检索
       ↓
     原文证据校验
       ↓
     ContextAssembler 按任务难度和回答深度组装有界上下文
       ↓
     Qwen 兼容模型流式生成；失败时只返回已定位原文
  ↓
SSE 推送状态、回答增量和结束事件
  ↓
对话历史 + 有界记忆写回 + SQLite 整体快照
```

这不是一个会自主反复决定“调用哪个工具—观察结果—继续调用”的通用 Agent Loop。当前是一个边界清楚的固定阅读问答流程，最多按代码预定路径做原文检索，然后生成回答。

## Windows 快速启动

### 1. 默认本地模式：SQLite＋filesystem，无需 Docker

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

打开 `http://127.0.0.1:8765/`。

默认开发账号：

```text
邮箱：reader@example.local
密码：reading-demo
```

默认数据位置：

- 应用快照：`var/dev-data/preview-state.sqlite3`
- 上传原文件：`var/dev-data/uploads/`

### 2. 启用真实模型

在启动前设置：

```powershell
$env:DASHSCOPE_API_KEY = "你的密钥"
# 可选；不设置时回答模型默认为 qwen3.7-plus
$env:READING_AGENT_MODEL = "qwen3.7-plus"
# 可选；不设置时意图模型默认为 qwen3.7-flash
$env:READING_AGENT_INTENT_MODEL = "qwen3.7-flash"
.\tools\start_stage05_preview.ps1
```

没有 `DASHSCOPE_API_KEY` 时：

- 意图识别会使用关键词规则降级；
- 普通聊天会显示有限的本地回答；
- 书籍问答会尽量定位原文，但不会生成完整的 AI 解释；
- `/healthz` 中的 `model_available` 为 `false`。

当前适配器名为 `QwenReaderModel`，请求格式使用 DashScope 的 OpenAI 兼容地址。回答模型和向量 embedding 分开配置；embedding 仅在显式开启后调用。

### 3. PostgreSQL＋MinIO 预览模式

只有本机服务已经存在且配置正确时才运行：

```powershell
.\tools\start_stage05_preview.ps1 -UseLocalServices
```

该参数会在环境变量未显式设置时使用：

```text
PostgreSQL: postgresql://postgres@127.0.0.1:15432/reading_agent_stage05
MinIO:     127.0.0.1:19000
Bucket:    reading-agent-stage05
```

也可以用 `.env.example` 中的变量覆盖。应用不会替你启动 PostgreSQL 或 MinIO。

启动后可以查看：

- `GET /healthz`
- `GET /api/v1/dev/status`

其中 `normalized_adapters_ready=true` 仅代表适配器已经构造完成；只有 `runtime_persistence_cutover=true` 才代表主运行时完全切换。当前代码默认后者为 `false`。

## 用户当前能体验到的功能

| 功能 | 状态 | 当前真实行为 |
| --- | --- | --- |
| 邮箱密码登录 | ✅ | 默认单一开发账号；HttpOnly 会话 Cookie，并校验 CSRF |
| 书架与导入 | ✅ | 可导入 PDF、EPUB、TXT、Markdown；导入后生成章节、Block 和 Chunk |
| 阅读器 | ✅ | 目录、正文、阅读进度、三栏布局、移动端页签 |
| 面板交互 | ✅ | 目录和“一起读”可收起；AI 面板可拖动调宽 |
| 连续对话 | ✅ | 不要求先划线；同一本书可沿用 `conversation_id` 继续追问 |
| 输入框提交体验 | ✅ | 发送后清空输入框，显示待发送消息和“正在理解”等阶段状态 |
| 流式回答 | ✅ | 后端通过 SSE 发送状态、工具、证据、回答增量和终态 |
| 停止生成 | ✅ | 前端可取消 Answer Run，并尝试关闭模型响应流 |
| 原文引用 | ✅ | 回答携带可验证 EvidenceRef，可从引用跳回原文位置 |
| 临时划选 | ✅ | 同一段落内选中文字后形成“仅本轮”的上下文卡，可解释或移除；不会自动保存为高亮 |
| 理解反馈 | ✅ | 最新回答提供“我还是卡住、举个例子、我试着复述、继续深入”入口，反馈沿用当前对话和概念 |
| 持久高亮 | 🟠 | 后端已有创建、列表、删除 API；当前网页没有完整的创建/编辑笔记流程 |
| 阅读外观设置 | ✅ | 主题、亮度、字号、行高、正文宽度；保存在当前浏览器 localStorage |
| AI 设置 | ✅ | 长期记忆开关、防剧透、语速、回答风格、深度、自定义表达偏好、书籍类型覆盖 |
| 按住说话 | ✅ | 使用浏览器 Web Speech API 把中文语音填进输入框；松手只停止识别，仍需用户发送 |
| 回答朗读 | ✅ | 使用浏览器 `speechSynthesis`；可调速度 |
| 定制 AI 声音 | ⬜ | 没有服务端 TTS、音色训练或统一温柔知性音色 |
| 实时双向语音 | ⬜ | 没有流式 ASR、打断、低延迟音频会话或全双工 Realtime 链路 |

## Agent 与意图识别

### 已接入

- `HybridIntentRouter` 接收当前问题、上一轮意图/问答、当前章节和显式引用。
- 有模型时，一次调用同时返回：
  - 路由：普通聊天、书籍对话、阅读器操作或需要澄清；
  - 问题关系：新问题、追问、困惑或纠正；
  - 任务和上下文需求；
  - 话题来源及把握程度；
  - 受限的认知判断，如理解、确认、深入、批判，以及困惑类型；
  - 有限回答策略，如解释、重建、验证、批判、应用、澄清或普通对话。
- 服务端不会直接信任模型输出：枚举、目标来源、话题来源、认知引文和置信度都会再次归一化。
- 认知判断只在书籍对话生效；普通闲聊不会判断“用户懂没懂”。
- 模型超时、无密钥、无效 JSON 或异常时，问题不会丢失，而是进入可观察的降级路由。

### 当前限制

- 降级路由本质上仍是关键词与规则，因此无模型时会出现用户之前看到的误判。
- “需要联网”的 `external_access` 目前只是意图结果中的计划字段，没有接上真正的联网搜索工具。
- 阅读器操作虽然能被识别为 `reader_action`，但当前不会自动操控前端完成跳章、改设置等动作。
- 当前没有离线评测驱动的路由校准、线上反馈学习或可训练分类器。

## 上下文组装与预算剪裁

`ContextAssembler` 已进入每次回答的主链路，当前规则是确定性的，不会额外调用模型：

- 总预算按普通聊天、验证、单段解释和跨段论证分档，并根据回答深度微调；当前运行范围约 1200–6500。
- 当前问题最多约 600 token。
- 阅读 Skill/用户偏好最多约 500 token。
- 先保留原文证据，再放书籍学习记忆，最后从新到旧放对话历史。
- 用户本轮显式选择的原文具有最高优先级；空间不足时会截短，但尽量不丢弃。
- 最近对话进入模型前最多取 4 轮。
- 书籍学习记忆的独立召回预算目前为 900 token。
- 剪掉了多少历史、记忆和证据会记录进 Trace。

当前还不是“完全动态”的上下文管理：不同意图会影响记忆计划与是否需要证据，但总预算和主要分配规则仍是固定算法；没有根据模型难度、成本或实时回答质量自动重新规划预算。

## 书籍检索与证据

### 默认已接入：词面检索

当前 `LocalBookToolProvider` 会：

- 把查询与 Chunk 文本拆成英文单词、中文连续串和中文双字片段；
- 按词面重叠计算分数；
- 默认取前 3 个 Chunk；
- 严格限制在当前用户和书籍版本内；章节问题再限制到对应章节，当前暂不按阅读进度裁剪；
- 为结果生成 EvidenceRef，并在回答前进行范围和内容摘要校验。

导入时目前采用“一条 Block 对应一条 Chunk”的预览策略。默认使用 BM25；显式开启后 `embedding_model` 记录 DashScope embedding 模型名称。

### pgvector 的真实状态

PostgreSQL migration 已经包含：

- `CREATE EXTENSION IF NOT EXISTS vector`；
- `chunks.embedding vector(1024)`；
- 关键词 GIN 索引；
- embedding 的 HNSW cosine 索引。

配置 embedding 后，运行时代码：

- 批量调用 DashScope embedding 模型并校验 1024 维有限值；
- 本地 sidecar 与 PostgreSQL `chunks.embedding` 均可保存向量；
- PostgreSQL 使用 scoped cosine 查询，本地使用 cosine fallback；
- BM25 与向量结果以固定 RRF 融合，异常时降级 BM25；
- PostgreSQL＋MinIO 下问答仍以本地对象为读取层，PG runtime 仍是部分切换。

因此当前状态是：**默认 BM25；配置后 DashScope embedding、本地/PG 向量检索与 RRF 已接线，PG runtime 仍部分切换。**

### 工具系统的真实状态

合同层定义了搜索书籍、读取原文 Block、读取目录、搜索阅读记忆、Web 搜索和读取网页来源等工具。

实际运行中：

- Provider 能处理 `search_book`、`read_book_blocks`、`get_book_structure`；
- 普通书籍问答固定调用 `search_book`，显式引用走本地 Block 读取路径；
- 模型不会自主挑选并反复调用这些工具；
- 阅读记忆搜索不是通用工具调用，而是回答 Handler 内部固定步骤；
- Web 搜索和网页读取会返回“未启用”；
- 没有 MCP 客户端、MCP Server 注册、权限确认或工具执行网关的真实接线。

## 记忆系统的真实状态

当前存在三层可运行数据，以及一套更深但尚未闭环的结构。

### 1. 对话历史：✅ 已接入

- 完成、失败或取消的 Answer Run 会保留用户问题、回答、引用、意图、模型名和 Trace 摘要。
- 同一 `conversation_id` 形成连续对话。
- 回答时最多取最近 4 个已完成回合。
- 默认 SQLite 模式会把终态回答写进整体快照，重启后可恢复。
- 会话 Cookie 本身不进入 SQLite 快照，所以重启后需要重新登录。

### 2. 简短书籍讨论记忆：✅ 已接入但较粗

`ReadingMemoryStore` 在记忆开关开启、且本轮没有写入结构化学习信号时，把完成的书籍回答压成：

```text
用户曾问：……
当时讨论：……
```

它严格按用户和书籍隔离，当前用关键词命中召回最多 3 条，并明确作为“对话背景”而不是原文证据。用户可以在设置中关闭或清除整本书的记忆。

限制：这不是语义记忆，没有去重、重要性判断、衰减、逐条编辑或跨书用户画像。

### 3. 结构化书籍学习记忆：🟡 基础闭环已接入，长期学习仍待完善

已经实现的数据结构包括：

- `BookConcept`：概念名、别名、章节来源、合并和退役状态；
- `LearningEpisode`：一次学习事件；
- `LearningSignal`：用户确认、纠正、困惑等信号及验证状态；
- `ConceptState`：某个概念在含义、论证、应用、批判等维度上的理解状态；
- `BookLearnerProfileView`：从概念状态和未解决问题动态聚合的书籍学习视图；
- `MemoryPlan`：由意图和认知结果生成的有界召回计划；
- `MemoryRetriever`：严格按用户、书、版本检索学习事件和概念状态；
- `BookMemoryWriter`：回答完成后的有界写回。

当前自动运行的部分包括：

- 用户明确困惑时，按用户原话、当前证据和上一轮概念懒加载或复用 `BookConcept`；
- 学习事件和信号绑定真实 `concept_id`，首次困惑仍保持为 `CANDIDATE`；
- 后续同概念反馈会确认前一困惑，并把明确理解、复述或纠正写成有界信号；
- 已接受信号会更新含义、论证、应用或批判维度的 `ConceptState`；
- 下一轮追问优先按上一概念召回状态，并用概念名补强“这个、继续”等模糊检索；
- 非书籍问题、记忆关闭、无明确学习信号、失败或取消的回答不会写入结构化学习事件。

尚未接通的关键闭环：

- 导入书籍时不会预生成全书概念图，未被用户讨论的概念没有锚点；
- 复述正确性目前依赖受限认知判断，没有独立的原文对照评测器；
- 概念别名合并、错误概念拆分和跨版本迁移仍需要更稳定的规则；
- 没有独立的全局长期用户画像、跨书偏好学习和遗忘策略。

所以当前更准确的说法是：**单本书、单概念的保守学习闭环已可运行；跨概念、跨书和长期演化仍未完成。**

## 阅读 Skill 与用户偏好

### 已接入

- 内置 7 类阅读方式：通用、哲学、历史、人文社科、科学科普、方法实用、小说叙事。
- 导入后会根据标题、目录和部分正文判断书籍类型：有模型时优先语义分类；没有模型时使用关键词降级。
- 对应阅读方法来自 `skills/reading/book_skills.v1.json`，会和用户偏好一起注入回答模型的 system context。
- 用户可在设置中覆盖自动书籍分类，并调整回答风格、深度和 400 字以内的表达偏好。
- 防剧透和检索阅读边界当前暂未接入；检索默认 BM25，配置后可使用向量混合检索。

### 当前限制

- 这不是完整的用户 Skill 编辑器；用户只能改少量偏好和自定义指令。
- 没有创建多个 Skill、版本管理、导入导出、共享、组合、条件触发或安全审查。
- 后端存在 teacher/friend/peer 角色字段，但当前网页没有暴露角色选择。
- 内置 Skill 是本地静态目录，不会自行学习或进化。
- 防剧透尚未接入；全书检索暂不施加阅读进度边界。

## 持久化与数据来源

### 默认 SQLite＋filesystem：✅

进程内对象是 API 当前读取层。每次成功的变更请求和回答结束后，系统把以下内容作为一个带校验摘要的整体快照写入 SQLite：

- 书籍、版本、章节、Block、Chunk 和 Chunk 文本；
- 原文件路径、阅读进度、高亮和证据；
- 终态 Job、终态 Answer、幂等记录；
- 阅读偏好、简短讨论记忆和结构化书籍记忆。

这能支持单机重启恢复，但不适合作为多实例生产架构。

### PostgreSQL＋MinIO：🟡/🟠 混合状态

配置后已经实际接入的部分：

- PostgreSQL migration 自动执行；
- 账号、密码哈希、会话和 CSRF 使用 `PostgresAuth`；
- 创建 Book 时写入规范化表；
- 完整 Version、Chapter、Block、Chunk 和初始进度可通过单事务写入；
- 发布 Book Version 使用数据库事务；
- Reading Progress 使用 `row_version` 乐观锁读写；
- Job 使用 PostgreSQL 存储、租约和原子领取合同；
- Answer Run、SSE Event 和终态结果写入规范化表；
- 上传原文件同时写入私有 MinIO bucket；
- 整体预览快照保存到 PostgreSQL，供当前进程重启恢复。

仍未完全切换的部分：

- `PersistenceBoundary.runtime_cutover_enabled` 默认是 `false`；
- 书籍列表、章节、Block、范围授权、问答检索和大部分历史读取仍通过进程内 `MemoryBooks/MemoryAnswers`；
- PostgreSQL 整体快照仍承担把这些进程内对象恢复出来的桥接职责；
- MinIO 只保存上传原文件，不负责解析产物、embedding 或其他派生对象；
- 没有多实例一致性、连接池治理、迁移发布流程或备份恢复工具。

## Job、Worker、恢复与多端

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| Job 状态机 | ✅ | 有 queued/running/terminal、checkpoint、lease、retry、cancel 合同 |
| PostgreSQL 原子领取 | 🟡 | 配置 PostgreSQL 后可由 `claim_next` 防止两个 Worker 同领任务 |
| 独立 Worker 类 | 🟠 | `JobWorker.run_once()` 已实现，但启动脚本没有启动 Worker 进程 |
| 导入异步执行 | 🟠 | API 返回 202 和 Job ID，但当前导入 Handler 实际在请求内同步执行 |
| 自动重试/恢复 | 🟠 | 有状态与接口；没有常驻 Worker 消费重试后的 queued Job |
| 删除任务完整执行 | 🟠 | 有墓碑、撤销和删除 Job 合同；没有独立清理 Worker 完成全部物理清理 |
| Answer 事件回放 | ✅ | SSE Ledger 可按序号回放；PG 配置时也写规范化事件表 |
| 进度冲突 | ✅ | `row_version + If-Match`；前端冲突时拉取新版本，不静默覆盖 |
| 多端实时同步 | ⬜ | 没有 WebSocket/推送、跨端会话状态同步或内容合并策略 |
| 多端偏好同步 | ⬜ | 阅读外观保存在浏览器 localStorage，仅当前浏览器可见 |

## 认证与安全边界

已经实现：

- Cookie 会话、CSRF、请求范围、用户/书籍/版本/章节隔离；
- 对外统一错误，不把栈和内部范围信息暴露给客户端；
- 上传大小、格式、幂等键和原文引用校验；
- Evidence 必须能从服务端当前范围重新读回，书籍回答才能完成；
- Trace 字段有脱敏函数，模型密钥只从环境变量读取。

当前仍是开发预览：

- 默认只有一个开发账号，没有注册、找回密码、邮箱验证、OAuth 或微信登录；
- SQLite 模式的会话不会跨重启保存；
- 没有生产级密钥管理、限流、滥用防护、审计后台和账号删除闭环；
- 没有外部工具权限确认或沙箱执行层。

## 尚未实现或不要误判为已实现

- 更完整的生产级 embedding/PG runtime 切换治理；
- DeepSeek 正式模型适配器；
- 联网搜索、网页读取和 MCP 工具接线；
- 模型自主多步 Agent Loop；
- 全局长期用户画像、跨书记忆、逐条记忆编辑和自动遗忘；
- 用户可编排的完整 Skill 编辑器；
- 高亮颜色、笔记、批注管理和知识卡片闭环；
- 独立后台 Worker、任务队列守护进程和跨进程自动恢复；
- 多端实时同步与离线合并；
- 训练音色、流式语音识别、全双工实时语音；
- OCR 扫描书识别；
- 云 OSS 的正式生产配置与运维；
- 正式账号体系、付费、部署和运营后台。

## 代码地图

| 位置 | 作用 |
| --- | --- |
| `web/src/App.tsx` | 当前全部网页交互、SSE、语音、阅读设置与三栏阅读器 |
| `src/reading_agent/api.py` | FastAPI 路由、会话、范围校验、书籍/进度/对话/记忆 API |
| `src/reading_agent/stage05.py` | 当前实际启动入口、上传处理、词面检索、模型适配器和回答运行时 |
| `src/reading_agent/dialogue.py` | 意图 V3、认知结果校验、语义模型与关键词降级 |
| `src/reading_agent/agent_core.py` | 上下文预算估算、排序和剪裁 |
| `src/reading_agent/memory.py` | 简短的书籍讨论记忆 |
| `src/reading_agent/book_memory.py` | 概念、学习事件、信号、理解状态与书籍学习视图 |
| `src/reading_agent/memory_plan.py` | 从意图生成记忆召回计划并做有界检索 |
| `src/reading_agent/memory_writeback.py` | 完成回答后的困惑事件与候选信号写回 |
| `src/reading_agent/companion.py` | 书籍自动分类、内置阅读 Skill、用户偏好 |
| `skills/reading/book_skills.v1.json` | 各书籍类型的内置阅读方法 |
| `src/reading_agent/contracts.py` | API、Intent、Tool、SSE、Book/Job/Answer 等严格数据合同 |
| `src/reading_agent/domain.py` | 范围、证据、工具结果、进度、高亮、删除等不变量 |
| `src/reading_agent/persistence.py` | SQLite/PostgreSQL 整体预览快照 |
| `src/reading_agent/postgres_adapter.py` | 规范化 Book、Progress、Job、Answer PostgreSQL 适配器 |
| `src/reading_agent/storage_boundary.py` | 快照桥接与规范化运行时切换状态 |
| `src/reading_agent/auth.py` | PostgreSQL 账号与持久会话 |
| `src/reading_agent/object_store.py` | 私有 MinIO 原文件存储 |
| `src/reading_agent/worker.py` | JobController 与尚未独立启动的有界 Worker |
| `experiments/f03_01_document_parsing/document_parser` | 当前运行时实际复用的 PDF/EPUB/TXT/Markdown 解析器 |
| `db/migrations/` | PostgreSQL、pgvector、证据、回答、快照和会话表结构 |

## 后续开发的合理接线顺序

如果目标是尽快让产品“更懂书、更懂用户”，应先补主链路，不应继续只增加接口：

1. **继续完善真实检索**：补充 embedding/PG runtime 的生产治理，同时保留无 embedding 时的 BM25 降级。
2. **再闭合书籍学习记忆**：生成稳定概念锚点，把后续确认/纠正关联到旧 Episode，验证 Signal 后更新 ConceptState，让 Profile 真正有数据可用。
3. **再完成 PostgreSQL 读取切换**：逐步让 Book、Chapter、Block、Answer History 和证据读取以规范化仓储为来源，移除整体快照的主运行时职责。
4. **再拆独立 Worker**：上传请求只创建 Job；Worker 负责解析、分块、embedding、校验和发布，并能从 checkpoint 恢复。
5. **最后扩展工具与语音**：在固定阅读链路稳定后，再接联网/MCP 和低延迟语音，避免把多个未闭环模块同时堆进运行时。

## 给后续开发者或 AI 的核对规则

判断一个功能是否真的完成时，请沿以下顺序检查：

```text
启动入口 create_stage05_app
  → 实际选择了哪个 Adapter/Provider
  → API 是否调用它
  → Handler 是否把结果交给下一步
  → 前端是否消费该结果
  → 重启后是否从同一个数据来源恢复
```

不要只因为 migration、Protocol、类名或测试存在就写“已实现”。如果只在测试中构造、只定义 Schema、只写入但不读取、或只有配置分支，必须按本文四种状态如实标记。

## 开源许可

本项目按 Apache License 2.0 发布，详见 `LICENSE`。密钥、数据库密码和对象存储凭证只能通过环境变量配置，不应提交到仓库。
