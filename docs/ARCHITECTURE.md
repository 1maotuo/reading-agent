# AI 阅读助手技术架构

- 状态：已验证架构基线；Stage 05 conversation-first 开发预览已实现，Gate 05未通过
- 版本：`0.2`
- 日期：2026-09-10

## 总体架构

```text
电脑网页 / 手机 PWA
        │ HTTPS + Session
        ▼
      Caddy
  ├─ React 静态页面
  └─ /api → FastAPI
                ├─ 账号与权限
                ├─ 书籍与阅读状态
                ├─ 确定性对话路由
                ├─ Agent Runtime
                ├─ 上下文与记忆
                ├─ 混合检索
                ├─ 工具网关与 MCP Client
                ├─ 证据门禁
                └─ Trace 与评测
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
 PostgreSQL + pgvector         独立 Worker
          │                    解析/切分/向量/发布
          ├─ 私有对象存储 OSS
          ├─ 千问模型 API
          ├─ Embedding/Reranker API
          ├─ ASR/TTS API
          └─ 联网搜索 MCP
```

## 技术边界

- 前端：React、TypeScript、Vite、React Router，响应式网页加 PWA。
- 后端：FastAPI 模块化单体，REST 处理普通请求，SSE 处理流式回答和任务进度。
- 数据库：PostgreSQL 作为唯一业务数据库，pgvector 保存向量。
- 文件：私有 S3 兼容对象存储；开发环境可用 MinIO，生产环境优先阿里云 OSS。
- 后台任务：PostgreSQL 任务表加独立 Worker，不引入 Redis 和 Celery。
- 部署：Docker Compose、Caddy、2核4G云服务器起步；不需要 GPU。

## Agent Runtime

采用一个主 Agent 和自研轻量 Runtime，不建立哲学、小说或检索子 Agent。

```text
用户请求
→ 后端校验 user_id、book_id、book_version、chapter/highlight/conversation
→ 零模型调用的 IntentFrame 路由
→ reader_action / clarification：确定性本地回应
→ open_dialogue：可选一次主模型调用，不强制书内检索
→ book_dialogue：注入阅读范围并执行书内工具
→ 工具网关校验注册、参数、权限、依赖、重复和调用预算
→ 本地工具或 MCP 执行
→ 结果标准化并写回
→ 主模型生成答案
→ 证据与格式门禁
→ 流式发布
→ 保存对话、Trace及允许更新的记忆
```

- 普通请求允许0至1轮工具调用，复杂请求最多2轮。
- 临时网络异常只对安全、幂等调用有限重试。
- 参数缺失优先从上下文和前置查询补充，仍缺失时询问用户。
- 空结果先检查实体、版本和范围；仍为空时如实说明。
- 关键证据不足时不生成确定性结论。
- `IntentFrame`只由服务端产生，当前四路为`reader_action`、`book_dialogue`、`open_dialogue`、`clarification`；客户端不能提交route或关闭Evidence Gate。
- 连续对话只召回同一用户、书籍、版本和conversation的最近四个`COMPLETED`轮次，并对每轮长度做硬截断；书内追问仍须当轮重新取证。

## 工具

模型看到统一的六个工具：

```text
search_book
read_book_blocks
get_book_structure
search_reading_memory
web_search
read_web_source
```

前四个为本地只读工具，后两个通过白名单 MCP 服务执行。Function Calling只负责生成结构化调用意图；Runtime负责真实调度。`user_id`、`book_id` 和阅读进度由后端注入，不由模型填写。

## 文档模型与解析

所有格式统一转换为：

```text
Book → Chapter → Section → Block
```

- PDF：后端使用 pdfplumber/pypdf 类解析器；前端原版模式使用 PDF.js。
- EPUB：按 manifest、spine 和 nav 解析。
- Markdown：使用 markdown-it-py 类解析器保留标题层级。
- TXT：处理编码、空行和段落边界。
- 第一版不做 OCR，扫描件返回明确的不支持状态。

原始 Block、检索 Chunk 和回答 EvidenceBundle 是三个不同对象：

- Block：稳定原文单元和定位锚点。
- Chunk：预先生成的检索单元，由同一小节内相邻 Block 组成。
- EvidenceBundle：查询时把相邻命中和必要前后文组合成证据包。

Chunk目标长度350至550 Token，硬上限约700 Token；不跨章节，尽量不跨小标题，只有拆分超长段落时保留少量重叠。

## 检索

```text
确定搜索范围
→ 关键词 Top10 + 向量 Top10
→ RRF 融合
→ 去重和章节/进度过滤
→ Reranker
→ 3至5个 EvidenceBundle
→ 必要时最多一次定向补取
```

- PostgreSQL保存全文检索字段，中文在应用层分词。
- pgvector保存 `qwen3.7-text-embedding` 的1024维向量。
- `qwen3.7-text-rerank`只用于章节级或全书级检索结果的精排。
- 暂不使用固定相似度阈值，等真实测试集建立后再校准。
- 记录模型、维度、chunker版本和book版本，不混用不同模型生成的向量。

## 上下文与记忆

发送给模型的 ContextPackage 顺序：

```text
Core Policy
→ 书籍类型 Skill
→ 当前任务策略
→ 用户 Skill / 表达偏好
→ 防剧透等策略
→ 当前任务和阅读锚点
→ 当前阅读状态
→ 必要的偏好与历史摘要
→ 书内/外部证据
→ 工具结果
→ 输出契约
```

典型预算2000至4000 Token，按 P0至P3优先级执行保留、提取、摘要和删除。全部划线独立持久化，每轮只动态召回相关内容。

第一版记忆包含：

- 当前阅读状态；
- 单本书的重要讨论、划线和未解决问题；
- 用户明确表达或多次稳定选择的交流偏好。

不让模型给用户生成“理解能力评分”，跨书知识网络暂缓。

## Skill

能力按四层组合，但并非每层都做成用户可见 Skill：

- Core Policy：证据、权限、隐私和输出安全等始终启用的内部硬约束，不允许用户覆盖。
- 书籍类型 Skill：通用、哲学理论、社科心理商业、科学科普、实用方法、小说叙事；后续按BookProfile动态选择。
- 当前任务策略：由服务端IntentFrame选择的解释、举例、论证分析、澄清或闲聊策略，属于内部运行时配置，不单独包装为Skill。
- 用户 Skill：后续允许用户编辑的表达方法和阅读偏好覆盖层，但不能改变事实、权限、Evidence Gate或数据范围。

书籍类型在导入阶段根据元数据、目录、前言和样本判断；置信度不足时使用通用 Skill，并允许用户覆盖。当前conversation-first切片只实现IntentFrame和任务策略，书籍类型自动选择与用户Skill编辑器仍延期。Skill只影响阅读方法和表达方式，不改变事实、权限和数据边界。

## 模型与语音

- 主模型：`qwen3.7-plus`，默认非思考模式；复杂综合、证据冲突和严谨论证才开启思考模式。
- Embedding：`qwen3.7-text-embedding`，1024维。
- Reranker：`qwen3.7-text-rerank`。
- ASR候选：`qwen-audio-3.0-asr-flash`。
- TTS候选：`qwen-audio-3.0-tts-plus`。

语音第一版后置：按住说话转成可编辑文字，回答由用户主动点击播放；不做全双工通话和声音克隆。

## 阅读器与划线锚点

- 默认统一流式阅读模式；PDF另有只读原版模式。
- 阅读器按章节懒加载，不渲染整本书。
- 阅读位置保存 chapter_id、block_id、block_offset，而不是像素位置。
- 划线同时保存位置选择器和文本引用选择器：Block范围、字符偏移、exact quote、prefix、suffix、来源位置和文本哈希。
- 划线不是提问前置条件；当前交互只把已校验Highlight作为下一轮可移除上下文，发送完成后自动清除临时引用但保留已保存划线。
- 恢复顺序：位置校验、Block内原文匹配、前后文匹配、章节内搜索；仍失败则保留笔记并标记孤立，不错误定位。

## 身份与同步

- 第一版账号/邮箱加密码和服务端 Session。
- 用户身份表预留 password、phone、wechat provider；正式公开前微信登录是必须项。
- Cookie使用 HttpOnly、Secure和SameSite策略。
- 数据统一绑定 user_id；数据库采用应用层校验，并预留Row Level Security作为纵深保护。
- `last_position`与`furthest_position`分开保存；多端更新带版本号、时间和设备ID。
- 第一版只做在线同步，IndexedDB仅作为缓存。

## 文件、任务与发布

```text
上传 → 校验 → 私有暂存 → 解析 → Block → Chunk
→ Embedding → 索引检查 → 原子发布 ready
```

- 原书只读，版本使用文件哈希和pipeline版本区分。
- Worker使用任务租约、心跳、检查点和幂等键。
- 新版本处理失败时，正式查询仍指向旧稳定版本。
- 用户删除书籍时清理原始文件、解析数据、向量和缓存，并按备份保留策略最终过期。

## Trace、评测和受控优化

- PostgreSQL保存业务Trace，JSON结构化日志记录系统错误，代码预留OpenTelemetry导出接口。
- Trace关联模型、Prompt、Skill、检索、工具、证据、Token、费用、耗时、错误和重试。
- 不在Trace重复保存整本原文，保存受控ID、哈希和必要脱敏摘要。
- 回答前校验证据存在、版本正确、归属当前用户、未越过剧透边界且真正支持关键结论。
- 系统级优化遵循：失败信号→候选策略→Badcase→全量回归→Gate→人工批准→版本发布→可回退。
- 第一版不允许系统未经评测直接修改线上Prompt、Skill或检索参数。
