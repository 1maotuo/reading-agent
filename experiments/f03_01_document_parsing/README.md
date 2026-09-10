# F-03-01：统一文档解析与来源锚点试验

本试验只回答一个高风险问题：PDF、EPUB、TXT 和 Markdown 能否被转换成同一套 `Document → Chapter → Section → Block` 结构，并让每个正文块稳定定位回原文件。

## 本轮验证范围

- 支持四种输入格式：`.pdf`、`.epub`、`.txt`、`.md/.markdown`。
- 统一输出章节、分节、正文块、稳定 ID、内容哈希和来源锚点。
- 同一文件使用同一解析版本重复解析，ID 和结构必须一致。
- fixture生成器必须字节确定：PDF固定元数据/ID，EPUB固定ZIP entry时间、顺序、属性和压缩；连续两次生成的所有输入文件SHA必须完全一致。
- 能依据锚点重新读取对应原文。
- 损坏 PDF、加密 PDF、无文本 PDF 和不支持格式必须返回明确错误，不能进入 `ready` 状态。

## 明确不代表

- 当前样本不能证明复杂双栏 PDF、公式、脚注、表格和所有 EPUB 均已兼容。
- 扫描 PDF 不在第一版范围，本试验只要求返回 `NO_EXTRACTABLE_TEXT`，不自动 OCR。
- 本目录是技术可行性原型，不是最终生产目录结构。
- 负面加密PDF只用于确定性错误分支测试，使用固定的RC4-40 fixture参数，不是生产加密建议。

## 运行

```powershell
$python = "<bundled-or-project-python>"
& $python scripts/generate_fixtures.py
& $python -m unittest discover -s tests -v
& $python run_experiment.py
```

结果写入 `artifacts/f03_01_report.json`，并按唯一run ID保存到 `artifacts/runs/<run_id>/report.json`。报告绑定当前源码、测试、runner、锁定依赖和fixture SHA；runner字段不是独立外层退出码，外层PowerShell必须单独捕获 `$LASTEXITCODE`。测试样本均由脚本生成，不包含受版权保护的书籍内容。
