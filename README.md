# 页伴｜AI 阅读助手

页伴是一个面向个人阅读的 AI 助手：用户可以导入 PDF、EPUB、TXT 或 Markdown，在网页中阅读、保存进度，并围绕书籍进行连续对话。产品目标有两个：帮助用户真正理解一本书，以及在长期阅读过程中形成自然的陪伴感。

这份 README 同时是当前源码的**真实实现与接线说明**。阅读代码时请严格区分：

- 一个类、接口、数据库表或测试文件存在，不等于它已经进入默认产品链路。
- 默认检索仍为 BM25；配置后 DashScope embedding、本地/PG 向量检索与 RRF 已接入。
- “有记忆数据结构”不等于系统已经形成完整的长期用户理解。
- 本文状态来自对启动入口、API、运行时和持久化调用链的静态核对，不根据历史计划推断。

当前源码快照日期：2026-09-12。

## 产品原则

页伴目前的技术设计围绕三个简单原则展开：

1. **书籍证据负责回答“书里到底说了什么”。** 书籍原文、页码、章节和引用必须能够从服务端重新读回并验证。
2. **用户记忆负责回答“这次应该怎样给这个用户讲”。** 记忆可以影响教学方式，但不能冒充书籍事实或原文证据。
3. **服务端掌握范围、工具和发布边界。** 模型可以帮助理解语义和组织语言，但不能自行指定用户、书籍、版本，也不能绕过工具参数和证据校验。

这三个原则决定了当前架构不是一个无限循环、自由调用工具的通用 Agent，而是一个固定、可观察、可降级的 Reading Agent Loop。

## 文档导航

- 想先运行项目：看“Windows 快速启动”；
- 想理解整体架构：看“当前默认运行链路”和“一条用户消息的完整生命周期”；
- 想理解 RAG：看“书籍检索与证据”；
- 想理解用户记忆：看“记忆系统的真实状态”；
- 想知道失败后会怎样：看“回答失败、取消、重连与恢复”；
- 想接着开发：看“代码地图”和“后续开发的合理接线顺序”。

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
  └─ 书籍问题：LocalBookToolProvider 做 BM25 或配置后的向量混合检索
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

## 核心模块与职责

| 模块 | 当前职责 | 不负责什么 |
| --- | --- | --- |
| React 网页 | 阅读、提问、划选、SSE 展示、停止回答、浏览器语音输入和朗读 | 不决定检索范围，不保存模型密钥 |
| FastAPI API | 登录、CSRF、资源归属、请求合同、幂等与接口响应 | 不让客户端指定内部用户或工具 Scope |
| `HybridIntentRouter` | 判断路由、追问关系、任务、话题和受限认知状态 | 不直接读取数据库，不执行工具 |
| `ReaderAnswerHandler` | 固定编排记忆、检索、上下文、模型、发布和写回 | 不是模型自主多步循环 |
| `LocalBookToolProvider` | 在当前用户/书/版本/章节范围内读取或检索书籍 | 不访问其他用户，不做 Web 搜索 |
| `ContextAssembler` | 在预算内保留问题、Skill、证据、记忆和最近对话 | 不额外调用模型做摘要 |
| `BookMemoryStore` | 保存概念、学习事件、学习信号并重建理解状态 | 不保存书籍事实，不跨书生成全局画像 |
| `AnswerEventLedger` | 管理 SSE 顺序、工具状态、证据状态和唯一终态 | 不自动重跑失败的模型或工具 |
| `PersistenceBoundary` | 在当前预览模式下统一保存和恢复整体状态 | 默认不代表 PostgreSQL 已完全切换 |

## 一条用户消息的完整生命周期

下面是当前源码中一条消息从网页到保存完成的真实路径。

```text
用户输入文字，或划选原文后输入问题
  ↓
前端 POST /api/v1/books/{book_id}/questions
  ↓
Session、CSRF、书籍归属、版本、章节与划选锚点校验
  ↓
读取同 conversation_id 的最近完成回合
  ↓
HybridIntentRouter：路由 + 任务 + 认知 + 当前话题
  ↓
创建 Answer Run，返回 run_id 与 conversation_id
  ↓
后台 ReaderAnswerHandler 开始固定流程
  ├─ 读取最多 4 个最近完成回合
  ├─ 找上一轮 concept_id，判断是否继续同一概念
  ├─ 搜索结构化学习记忆；无命中时才查简短讨论记忆
  ├─ 生成本轮教学动作与动态上下文预算
  ├─ 明确划选：读取原文 Block
  └─ 普通书籍问题：搜索书籍 Chunk
       ├─ 默认 BM25
       └─ 配置后 BM25 + embedding + RRF
  ↓
服务端重新读回 Evidence，验证用户/书/版本/章节/哈希
  ↓
ContextAssembler：证据优先，之后是学习记忆和最近历史
  ↓
Qwen 兼容模型流式生成
  ↓
引用守卫过滤不存在的 [E#]，缺引用时确定性补合法引用
  ↓
AnswerEventLedger 进入 completed / failed / cancelled 唯一终态
  ↓
成功书籍回答尝试写入结构化学习记忆
  ├─ 有明确学习信号：Concept → Episode → Signal → State
  └─ 无结构化信号：写一条简短讨论摘要
  ↓
保存 Trace 与 SQLite/PostgreSQL 预览快照
```

### 1. 前端提交什么

一次问题请求的主要字段是：

```text
question             用户原始问题
conversation_id      可选；沿用同一本书的连续对话
current_chapter_id   可选；用户当前正在看的章节
selection_context    可选；本轮临时划选的原文、位置和哈希
highlight_id         可选；已保存高亮
client_request_id    客户端生成的请求 ID
Idempotency-Key      HTTP 幂等键
```

客户端不能提交 `user_id`、`book_version_id`、内部 Scope、最终路由或工具权限。它们全部由服务端从 Session、Book 和当前活动版本推导。

### 2. 请求如何绑定安全范围

API 在进入回答流程前，确认当前 Session 有权访问书籍，并把请求绑定为：

```text
user_id + book_id + book_version_id + optional chapter_id + request_id + trace_id
```

划选文字还会校验 Block、偏移量、原文和 SHA-256。对话 ID 如果属于其他用户、其他书或旧版本，对外统一表现为资源不存在，不泄露真实归属。

### 3. 意图与认知如何影响后续步骤

`HybridIntentRouter` 一次产生一个受限的 `IntentFrame`：

- `route` 决定是普通聊天、书籍问答、阅读器操作还是澄清；
- `relation` 判断是新问题、追问、继续困惑还是纠正；
- `scope/tasks/context_needs` 决定需要当前段落、当前书、最近对话或外部来源；
- `cognition` 保存当前话题、学习目标、可能的理解状态和卡点类型；
- `response_strategy` 把判断压缩为解释、重建、验证、批判、应用或澄清等回答策略。

有模型时由轻量模型返回结构化 JSON；无模型、超时或结果非法时使用确定性降级。模型给出的认知引文必须能在当前或近期用户原话中找到，否则会被丢弃。

### 4. 如何处理“这个、刚才那个、继续”等模糊追问

如果当前消息被判断为追问，运行时会查看上一完成回合 Trace 中的 `concept_id`。当新消息没有明确切换概念时，会沿用上一概念，并把检索查询改写为：

```text
上一概念规范名 + 当前用户问题
```

例如“那为什么会这样？”可以改写为“洞穴寓言 那为什么会这样？”。如果当前消息明确提出了一个不同概念，旧概念不会被复用。

### 5. SSE 如何把过程返回前端

创建问题后，前端通过：

```text
GET /api/v1/answer-runs/{run_id}/events
```

订阅事件。服务端按递增 `seq` 发送：

```text
accepted
status(understanding/searching/generating/saving)
tool_started
tool_finished
evidence
answer_delta × N
completed | failed | cancelled
```

Ledger 禁止工具重复在途、证据之后再调用工具、终态之后继续追加事件，也支持通过 `Last-Event-ID` 回放遗漏事件。当前前端遇到 `EventSource.onerror` 会主动关闭连接，因此服务端回放能力尚未在网页端充分利用。

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

### 3. 可选：启用书籍向量检索

向量检索默认关闭。要让新导入的书籍生成 embedding，需要同时提供 DashScope 密钥并显式开启：

```powershell
$env:DASHSCOPE_API_KEY = "你的密钥"
$env:READING_AGENT_ENABLE_EMBEDDINGS = "1"
.\tools\start_stage05_preview.ps1
```

开启后，导入会调用 DashScope embedding 接口并校验每条向量为 1024 维有限数值。SQLite 模式把向量随预览状态保存，本地查询使用 cosine；配置 PostgreSQL 时优先尝试 scoped pgvector 查询。任一向量环节失败都会回退到仍然可用的 BM25。

注意：已经在关闭 embedding 时导入的旧书不会自动补齐向量；当前没有独立的批量重建索引命令。

### 4. PostgreSQL＋MinIO 预览模式

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
| AI 设置 | 🟠 | 长期记忆、语速、回答风格、深度、自定义表达偏好和书籍类型覆盖已生效；防剧透值可保存但检索边界尚未接入 |
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

### 一次书籍检索如何产生引用

书籍导入后，运行时保留 `Book → Version → Chapter → Block → Chunk` 的层级。目前预览导入通常让一条 Block 对应一条 Chunk；搜索命中主要 Chunk 后，还会在同一章节内补充前后相邻 Chunk，避免把一个连续论证截成孤立句子。

假设用户先问“洞穴寓言中的影子为什么代表表象？”，下一轮只说“那为什么会这样？”，当前流程是：

```text
意图识别判断为上一轮追问
  ↓
从上一轮 Trace 取出“洞穴寓言”的 concept_id
  ↓
查询改写为“洞穴寓言 那为什么会这样？”
  ↓
BM25 取字面相关候选
  +
可选 embedding 取语义相关候选
  ↓
RRF 合并，不直接比较两种分数的绝对值
  ↓
保留前 3 个主命中，并补同章节相邻 Chunk
  ↓
每个结果生成 EvidenceRef
```

`EvidenceRef` 记录用户、书、版本、章节、Chunk、Block、原文、哈希和来源位置。回答前，`issue_verified_evidence_token` 会通过服务端 Evidence Reader 重新读回这些记录；只有范围和内容校验通过后，Ledger 才允许发送 `evidence`、`answer_delta` 和 `completed`。

回答模型看到的是 `[E1]`、`[E2]` 等本轮局部编号。流式发布时 `_CitationStreamGuard` 会缓冲可能被拆开的引用标记并删除超出本轮 Evidence 数量的编号；结束时 `prepare_answer_for_publish` 再做一次确定性检查。这个校验确认“引用编号确实存在”，但不等同于完整的事实一致性或论证正确性评测。

### 当前检索质量边界

- 一次普通书籍问题主要围绕一个查询取 Top 3，不会自动拆成多路子问题；
- 没有独立 cross-encoder/reranker 判断“语义相似的段落是否真的能回答问题”；
- 跨章节长论证主要依赖全书检索结果，不会自动构建多跳论证链；
- 当前相邻扩展只在同一章节内进行，不跨章节拼接；
- Web 搜索没有启用，因此 `[E#]` 当前只代表本地书籍证据。

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

### 记忆不是书籍知识库

当前运行时严格区分两条检索线路：

```text
书籍知识检索：回答“书里到底写了什么”，产出可引用 Evidence
用户记忆检索：回答“这个用户之前哪里没懂、这次应该怎样讲”，只作为背景
```

结构化学习记忆目前不使用向量数据库。已知当前概念时，按 `user_id + book_id + book_version_id + concept_id` 精确读取，比相似度搜索更稳定；概念尚未锁定时，才用当前问题与概念名、学习事件摘要的中英文词项重叠进行排序。跨书、跨概念语义记忆增多以后才有引入向量召回的必要。

### 回答前：记忆如何被搜索

```text
当前问题 + IntentFrame
  ↓
判断是否沿用上一轮 concept_id
  ↓
MemoryPlanner 生成有界计划
  ├─ 非书籍路由：不读书籍学习记忆
  ├─ 已知概念：先查 ConceptState，再查 LearningEpisode
  └─ 未知概念：先按问题查 Episode，再看相关 State
  ↓
MemoryRetriever 先做用户/书/版本隔离，再排序
  ↓
最多 1 条概念状态 + 3 条学习事件，总预算 900 token
  ↓
有结构化命中：不再加载旧讨论摘要
无结构化命中：ReadingMemoryStore 最多返回 3 条关键词相关摘要
```

概念精确命中拥有最高权重；之后考虑问题文字重叠、`blocked/partial/verified/conflicted` 等状态和时间。`unknown` 状态不会作为有用记忆返回。结果会被压缩成短 `MemoryHit`，例如：

```text
概念「机会成本」的 relation 理解状态：blocked
用户曾问：为什么不是计算所有放弃的东西？（策略：example）
```

进入模型前，记忆会被显式包装为“书籍学习记忆；仅作背景，不是原文证据”。最终 `ContextAssembler` 仍然优先保留书籍 Evidence；预算不足时可以裁掉记忆或旧历史，但尽量不丢用户本轮明确选择的原文。

### 回答后：什么会写入记忆

只有同时满足以下条件，`BookMemoryWriter` 才尝试写结构化学习记忆：

```text
书籍问答 + 回答成功完成 + 长期记忆开启 + 存在明确学习信号
```

当前可写信号包括明确困惑、用户纠正、明确自述理解，以及受限认知判断识别出的复述、重建或应用表现。普通总结、无明确学习表现的闲聊、失败和取消回答不会写入结构化学习事件。

写入顺序是：

```text
resolve_concept
  ↓ 复用同名/别名概念，或为明确新话题建立临时概念
LearningEpisode
  ↓ 记录问题、学习目标、卡点、回答策略和 Evidence ID
LearningSignal
  ↓ 记录正/负方向、强度、来源和 candidate/accepted 状态
ConceptState
  ↓ 从已接受信号重建 meaning/argument/application/critique/relation 状态
```

首次明确困惑通常先保持为 `CANDIDATE`；下一次同概念反馈会确认前一困惑，再应用新信号。单纯说“我懂了”最多支持保守的 `partial`，不会直接升级为 `verified`。`verified` 应依赖可观察的正确复述、重建或应用，但当前还没有独立的原文对照复述评测器。

Answer 会先进入完成终态，再尝试记忆写回。记忆写回异常只在 Trace 中记录 `writeback_failed`，不会让用户已经得到的回答变成失败。如果本轮没有结构化信号，系统才保存一条简短讨论摘要，避免两套记忆重复写入。

### 三轮对话示例

```text
第 1 轮：用户说“我不懂机会成本为什么只算最佳替代选项”
  → 没有旧概念记忆
  → 检索书籍 Evidence 并解释
  → 创建“机会成本”概念、学习事件和候选困惑信号

第 2 轮：用户说“还是没懂，其他放弃的东西难道不是成本吗”
  → 沿用上一轮 concept_id
  → 召回上一困惑，检索查询补上“机会成本”
  → 教学策略要求换解释路径，不重复定义
  → 确认上一困惑，更新 relation 维度为 blocked

第 3 轮：用户说“我理解是不是：读书时放弃的最佳工作机会才是机会成本”
  → 召回 relation=blocked 和前两次 Episode
  → 以相关原文回答并检查这次复述
  → 写正向信号，保守更新为 partial；独立校验完成后才适合 verified
```

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

## 回答失败、取消、重连与恢复

回答运行时采用“能安全降级就继续，缺少书籍证据就失败”的原则。当前没有对回答模型或书籍工具做自动重复调用。

| 故障位置 | 当前行为 | 是否自动重试 |
| --- | --- | --- |
| 意图模型无密钥、超时或非法 JSON | 使用关键词和规则生成降级 Intent，问题继续处理 | 否，直接降级 |
| embedding/pgvector 失败 | 尝试本地向量；仍失败时继续 BM25 | 否，逐级降级 |
| 书籍搜索没有 Evidence | 工具标记失败，Answer 进入 `failed`，不让模型无证据编造 | 否 |
| 回答模型在输出前失败 | 书籍问题返回“模型不可用＋最相关原文”的本地答案；普通聊天返回本地提示 | 否，直接降级 |
| 回答模型输出部分内容后失败 | 不拼接另一套兜底答案，Answer 进入 `failed` | 否 |
| 记忆写回失败 | Answer 保持 `completed`，Trace 记录失败 | 否，不影响回答 |
| 用户点击停止 | 设置取消标志并关闭活动模型响应，Ledger 进入 `cancelled` | 不适用 |
| SSE 连接中断 | 服务端 Ledger 可按 `Last-Event-ID` 回放；当前网页会关闭出错连接 | 前端尚未充分自动恢复 |
| 进程在回答中硬退出 | 重启后恢复最近合法快照；在途生成不会自动续跑 | 否，用户需重新提问 |
| 重复提交相同幂等键和请求体 | 返回原 Answer Run，避免创建重复任务 | 这是去重，不是重算 |

`AnswerEventLedger` 只允许一个终态：`completed`、`failed` 或 `cancelled`。工具在途时不能用相同 `call_id` 重试，终态以后也不能继续发送 Answer Delta。PostgreSQL Answer Store 配置存在时，事件先写入持久化 Sink，再进入进程内回放列表；默认 SQLite 模式则在请求和回答结束时保存整体快照。

导入和删除使用另一套 Job 状态机，已经定义 queued/running/lease/checkpoint/retry/cancel，但启动脚本当前没有常驻 Worker 自动消费重试后的任务。因此 Answer 重试和 Job 重试不要视为同一个能力。

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

## 常用环境变量

| 变量 | 默认值/行为 | 用途 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | 空；模型能力降级 | 意图、回答、书籍分类和可选 embedding 的凭证 |
| `READING_AGENT_MODEL` | `qwen3.7-plus` | 回答模型名 |
| `READING_AGENT_INTENT_MODEL` | `qwen3.7-flash` | 结构化意图与认知模型名 |
| `READING_AGENT_INTENT_TIMEOUT_SECONDS` | `5` | 意图调用超时，运行时限制在安全范围内 |
| `READING_AGENT_ENABLE_EMBEDDINGS` | `0` | 设为 `1` 才启用导入 embedding 和混合检索 |
| `READING_AGENT_PREVIEW_EMAIL` | `reader@example.local` | 本地预览账号 |
| `READING_AGENT_PREVIEW_PASSWORD` | `reading-demo` | 本地预览密码 |
| `READING_AGENT_PREVIEW_DATA_ROOT` | 项目根目录 | SQLite 快照和本地上传文件根目录 |
| `READING_AGENT_STAGE05_POSTGRES_DSN` | 空 | PostgreSQL 预览连接；兼容 `READING_AGENT_STAGE05_DSN` |
| `READING_AGENT_STAGE05_MINIO_ENDPOINT` | 空 | 配置后启用 MinIO 原文件镜像 |
| `READING_AGENT_STAGE05_MINIO_ACCESS_KEY` | 空 | MinIO Access Key |
| `READING_AGENT_STAGE05_MINIO_SECRET_KEY` | 空 | MinIO Secret Key |
| `READING_AGENT_STAGE05_MINIO_BUCKET` | `reading-agent-stage05` | 私有 Bucket 名称 |
| `READING_AGENT_STAGE05_MINIO_SECURE` | `0` | `1` 表示使用 HTTPS 连接 MinIO |

## 主要 HTTP 接口

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/v1/sessions` | 登录并建立 Cookie Session |
| `GET /api/v1/session` | 查询当前 Session |
| `GET/POST /api/v1/books` | 书架列表与上传导入 |
| `GET /api/v1/books/{book_id}/chapters` | 分页读取目录 |
| `GET /api/v1/books/{book_id}/chapters/{chapter_id}/blocks` | 分页读取章节正文 |
| `GET/PUT /api/v1/books/{book_id}/progress` | 读取或乐观锁更新进度 |
| `GET/POST/DELETE /api/v1/books/{book_id}/highlights...` | 高亮合同；网页创建流程尚未完整接入 |
| `POST /api/v1/books/{book_id}/questions` | 创建 Answer Run 并返回 Intent |
| `GET /api/v1/answer-runs/{run_id}/events` | SSE 状态、工具、证据和回答流 |
| `POST /api/v1/answer-runs/{run_id}/cancel` | 取消运行中的回答 |
| `GET /api/v1/answer-runs/{run_id}/evidence` | 读取已验证 Evidence Bundle |
| `GET /api/v1/books/{book_id}/answers` | 读取当前书籍回答历史 |
| `GET /api/v1/books/{book_id}/book-memory` | 动态生成结构化学习画像视图 |
| `DELETE /api/v1/books/{book_id}/memories` | 清除该书简短记忆和结构化学习记忆 |
| `GET /api/v1/traces/{trace_id}` | 查看安全白名单内的运行 Trace |

除登录等建立会话的入口外，已认证的非只读请求都需要 CSRF；上传、问题、高亮、Job 重试等可重复操作使用幂等键或明确状态约束。OpenAPI 来自同一套 Pydantic 合同，但前端 `types.ts` 目前仍为手工维护，并非自动生成。

## 开发与验证

后端测试按能力分在 `tests/contracts`、`tests/stage05` 和 `tests/stage06`。大部分测试使用内存对象或临时 SQLite；真实 PostgreSQL、MinIO、浏览器 E2E 和付费 DashScope 调用需要单独环境，不应混进每次小改动的默认验证。

常用的低成本检查：

```powershell
# 与当前改动直接相关的 Python 测试
.\.venv\Scripts\python.exe -m pytest tests\stage05\test_agent_core_v1.py -q

# 记忆闭环
.\.venv\Scripts\python.exe -m pytest tests\stage05\test_book_memory_v1.py tests\stage05\test_memory_plan_v1.py tests\stage05\test_memory_writeback_v1.py -q

# 检索
.\.venv\Scripts\python.exe -m pytest tests\stage05\test_retrieval_v1.py tests\stage05\test_retrieval_v2.py -q

# 前端类型检查与生产构建
Set-Location web
pnpm run build
```

`test_live_dashscope_embedding` 属于真实付费/外部调用路径，默认测试不会自动执行。PostgreSQL 适配器测试需要可连接并安装 pgvector 的真实数据库。

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
| `src/reading_agent/memory_writeback.py` | 完成回答后的困惑、理解、复述和纠正信号写回 |
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

当前检索、上下文和单书学习记忆已经进入主链路。下一阶段应继续围绕“理解用户”和“陪伴阅读”，而不是先扩建通用平台能力：

1. **补理解校验**：用户主动复述、回答测试题或应用概念时，用相关原文做一次受限判断，只返回正确、部分正确、存在误解或无法判断，并据此决定是否把 ConceptState 升级为 `verified`。
2. **重构前端交互骨架**：把当前集中在 `App.tsx` 的阅读、回答流、设置和语音拆成少量组件与 Hook；先让 Answer/SSE 生命周期清楚，再重做视觉和陪伴交互。
3. **让语音复用同一 Reading Agent Loop**：语音只负责听、说、打断和低延迟体验，最终文本仍进入同一意图、记忆、检索、证据和写回链路，避免出现第二套没有书籍依据的“语音大脑”。
4. **按真实发布需求再补基础设施**：多实例或大规模导入出现之前，不优先做完整 PostgreSQL Cutover、独立 Worker、通用 MCP 网关和跨书画像；需要时再逐项接入。

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
