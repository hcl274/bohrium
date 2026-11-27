# pdf-paper-to-deck — 文献文档解析 → 可编辑学术 PPT

把科研论文 PDF 解析成"可回到原文页码/区块"的结构化结果，并生成可编辑 PPTX：每页幻灯片对应论文的某节/某图/某表，保留页级证据映射、讲稿备注与真实渲染预览。

本作品用于"阅读01｜文献文档解析"赛题（产品周赛 · 郑大分站赛 · 第一期）。只提交 Skill 本体与必要资源；正式案例与对照由平台提供。

## 方法

分四步，全部由脚本确定性地完成，Agent 只在脚本之间做"读题、OCR 兜底、可选提炼"：

1. **解析（parse）**：`pdfinfo` 拿页数、探测文字层；文本提取按 `PyMuPDF → pdfplumber → pdftotext -bbox → pdftotext -layout` 阶梯降级；用投影剖面法（列墨迹直方图找栏间空白谷）判定双栏并做"左栏自上而下 → 右栏自上而下"的列主序还原；再用正则把块分类为标题/正文/图注/公式/参考文献。
2. **成片（build）**：把结构组装成 PPTX（标题页 → 摘要页 → 章节页 → 表格页 → 图页 → 公式页 → 参考文献页），每页备注里写回"原文逐字片段 + 页码/区块定位"；同时产出提纲、讲稿、证据映射 JSON。
3. **渲染（render）**：`soffice --headless` 把 PPTX 转 PDF，`pdftoppm` 出 PNG 预览，供检查。
4. **收尾（finalize）**：逐字校验证据 quote、计算覆盖率、组装 16 字段 `result.json`，并原样回填输入的 `output_contract`。

## 依赖与安装

- **必装（运行时环境工具）**：`pdftotext`、`pdfinfo`、`pdftoppm`（poppler-utils）、`soffice`/`libreoffice`（渲染预览）、`tesseract`（扫描件 OCR 兜底）。
- **Python 3（仅标准库即可运行）**。可选增强库按需 try/except：`PyMuPDF`（fitz）、`pdfplumber`、`python-pptx`。缺失时自动降级到 `pdftotext -bbox` 与内置最小 OOXML 生成器，功能可用但表格/图片精度下降。

安装可选依赖（非必需）：

```bash
python3 -m pip install PyMuPDF pdfplumber python-pptx
```

## 使用

运行时输入 `/input`（`case.json` + `task.md` + 论文 PDF），输出 `/app/output`。设 `SKILL_ROOT` 为本目录：

```bash
python3 "$SKILL_ROOT/scripts/parse_pdf.py" run
python3 "$SKILL_ROOT/scripts/build_deck.py"
python3 "$SKILL_ROOT/scripts/render_preview.py"
python3 "$SKILL_ROOT/scripts/finalize.py"
```

环境变量覆盖（可选）：`WEEKLY_INPUT_DIR`、`WEEKLY_WORK_DIR`、`WEEKLY_OUTPUT_DIR`（默认 `/input`、`/app/work`、`/app/output`）。

## 输入输出

- 输入：`case.json`（含 `case_id` 与可选的 `output_contract`）、`task.md`、论文 PDF（可无文字层）。
- 输出：`result.json`（16 字段）、`deck.pptx`、`deck.pdf` + `previews/page-*.png`、`extracted_text.md`、`outline.md`、`speaker_notes.md`、`evidence_map.json`。

## 适用范围与限制

- **适用**：双栏/单栏学术论文 PDF（有文字层）；正文、章节、表格、图表题注、公式编号、参考文献的结构化提取与页级定位。
- **限制**：公式与特殊字符按"结构元素"识别，不做语义推导；扫描件依赖 `tesseract` 及语言包（`eng`/`chi_sim`）；跨页表格先按页输出，跨页合并需 Agent 二次处理；加密 PDF 无法提取时如实报告并转人工。

## 失败处理与自查

- 任何脚本失败都不会中断流程：错误写入 `result.json.errors`，降级写入 `degradations`，全部脚本退出码恒 0。
- 空输入 / 坏 JSON / 缺 PDF / 无文字层四种负例均须仍产出合法 `result.json`。
- 提交前按 `SKILL.md` 第 12 节自检清单逐项核对。

## 许可证

MIT。本作品不含任何正式案例答案、预生成测评结果或运行日志。
