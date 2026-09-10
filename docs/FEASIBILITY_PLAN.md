# 第03阶段可行性试验计划

- 状态：进行中；F-03-01、F-03-03和F-03-04已在限定实验边界通过；F-03-02可信`chunk_index`代码合同通过但真实数据库仍blocked，因此Gate 03保持不通过。后续按严格但有界策略推进，不再扩大已完成实验的审计范围
- 日期：2026-09-08
- 目标：验证高风险技术假设，不开发完整产品。

## F-03-01 统一文档解析与来源锚点

### 问题

PDF、EPUB、TXT和Markdown能否转换为统一的Chapter/Section/Block模型，并在网页回答中定位回原文。

### 最小试验

- 为四种格式准备内容等价、结构明确的合法测试样本。
- 解析标题、段落、列表和来源位置。
- 生成稳定Block ID和内容哈希。
- 对同一文件重复解析两次，比较Block顺序、文本和ID。
- 连续生成两次完整fixture目录，比较每个输入文件的SHA-256；PDF元数据/对象ID和EPUB ZIP entry时间戳、顺序、属性、压缩参数必须固定。
- 对损坏、加密或无文本PDF返回明确错误。

### 通过门槛

- 四类格式均得到符合Schema的统一输出。
- 同一输入、同一pipeline版本的稳定Block ID一致。
- 连续两次生成的所有输入fixture字节完全一致；确定性检查必须是正式自动测试，不得手工改写hash。
- 任意测试段落可通过来源锚点定位。
- 不支持文件不会进入ready状态。

## F-03-02 PostgreSQL与pgvector混合检索

### 问题

单一PostgreSQL能否在第一版规模下同时承担业务数据、中文关键词检索和向量检索。

### 最小试验

- 启动带pgvector的PostgreSQL。
- 导入试验Block和Chunk。
- 对一组精确实体问题和语义改写问题分别运行关键词与向量检索。
- 使用RRF融合，按需调用Reranker。
- 验证`user_id`、`book_id`、`document_version`、`reading_scope`、章节、`block_id`、`chunk_id`和可信`chunk_index`；数据库正文须由独立oracle认证后才可进入外部Reranker。
- 对关键词、向量、RRF和Rerank逐路验证命中；任一路真实未命中或候选越界都必须失败关闭。
- 在代表性约10,000 Chunk真实数据库上重复采样并记录p95；检查预期索引和EXPLAIN中的Seq Scan。低样本、零索引、Seq Scan或仅provider连通性均不能替代数据库证据。

### 通过门槛

- 精确词和语义改写均能在Top-K命中预期证据。
- 所有结果都满足用户和书籍范围过滤。
- 能输出排名、来源Block和可测量延迟。
- 规模、重复p95、预期索引和EXPLAIN为硬门禁；没有真实数据库运行时整体状态必须为blocked，不能使用合成契约结果冒充通过。
- 若真实Embedding凭证缺失，模拟结果只能标记“接口契约通过”，不得标记模型效果通过。

## F-03-03 工具调用、SSE和有限循环

### 问题

主模型能否在不使用LangChain/LangGraph的情况下完成直接回答、结构化工具调用、结果回写和流式发布。

### 最小试验

- 定义一个本地书内搜索工具和Pydantic参数Schema。
- 准备直接回答、需要检索、参数缺失和工具超时四类请求。
- Runtime执行最多两轮工具调用。
- FastAPI通过SSE输出状态、文本增量、引用和最终状态。

### 通过门槛

- 工具名称和参数通过Schema校验。
- 缺参、超时、空结果和格式错误进入正确分支。
- SSE事件顺序稳定，断开后不会把未完成任务写成成功。
- 真实模型未调用时，只能证明Runtime契约，不能证明模型选择准确率。

## F-03-04 划线锚点恢复

### 问题

用户改变字体、屏幕宽度、刷新或重新登录后，划线是否仍能回到正确原文。

### 最小试验

- 渲染包含重复句子和多段选择的测试章节。
- 保存Block位置、字符偏移、exact quote、prefix和suffix。
- 以浏览器原生`Selection.toString()`保存可见quote，同时保存raw DOM文本节点坐标、raw quote和raw上下文；明确禁止把渲染文本长度直接当作raw DOM offset。
- 第一版禁止嵌套正文`data-block-id`；capture/restore发现嵌套、缺失或不匹配的`readerId`/`bookId`/`documentVersion`必须结构化拒绝。
- 测试刷新、字号变化、桌面宽屏和手机窄屏。
- 模拟同版本轻微排版变化和新版本正文变化。
- 测试`display:none`、`hidden`、`visibility:hidden`等不可见DOM文本与相邻可见文本；native quote正确但raw quote/端点不一致时必须继续唯一判定或安全拒绝。

### 通过门槛

- 同版本四类界面变化全部恢复正确。
- 位置失效时能通过文本与前后文恢复。
- 匹配有歧义时不错误绑定，标记为待重新定位。
- 端点恢复前必须属于合法Block且offset非负、不越界；重叠文本、标题诱饵、上下文冲突和malformed anchor均fail closed；恢复后再次原生capture必须与返回quote/身份/坐标一致。

## 证据要求

每项试验必须保存：

- 试验代码和样本说明；
- 可复现命令；
- 执行日期和退出状态；
- 关键结果摘要；
- 相关文件哈希或Git提交；
- 未验证部分与限制；
- Gate结论。
- 报告必须绑定唯一run ID、UTC、源码/测试/runner/依赖和artifact哈希，并区分runner退出字段与外层OS退出码；失败run不得覆盖当前成功artifact。

## 执行顺序

```text
F-03-01 文档解析
→ F-03-02 检索
→ F-03-03 Runtime与SSE
→ F-03-04 划线恢复
→ 汇总Gate 03
```
