---
name: pdf-paper-to-deck
description: 文献文档解析与论文转可编辑学术 PPT。把科研论文 PDF 逐页解析为带页码/区块定位的结构化结果（正文、多栏阅读顺序、章节层级、表格、图表题注、公式、参考文献），再组织成可编辑 PPTX，每页幻灯片保留页级证据映射、讲稿/备注，并渲染真实 PDF/PNG 预览。触发词：文献解析、PDF 解析、论文转 PPT、学术汇报、阅读/读书笔记、多栏排版、表格提取、公式提取、扫描件 OCR。当输入包含科研 PDF 且要求产出结构化结果或可编辑幻灯片时使用；不用于纯文字闲聊或与 PDF 无关的写作。
version: 1.0.0
license: MIT
---

# 文献文档解析 → 可编辑学术 PPT

## 1. 用途与触发

**做什么**：把一个科研论文 PDF 读成"能回到原文页码/区块"的结构化结果，再生成一份**可编辑 PPTX**，让每一页幻灯片都对应论文的某页/某节，并保留讲稿备注与渲染预览。

**触发**：输入目录 `/input` 下有 `case.json`、`task.md` 和论文 PDF，任务要求"文献文档解析 / 阅读 / 提取 / 结构化 / 做汇报 PPT"。

**不触发**：没有 PDF 材料；任务只要求聊天式问答且不需要结构化落盘。

**核心承诺**（对应题面四个要求）：

- **提得全**：正文、图注、脚注、参考文献逐字提取，页眉页脚单独标注、不混入正文。
- **排得对**：双栏/多栏按"左栏自上而下 → 右栏自上而下"还原，不串栏、不把相邻段拼成一句。
- **拆得清**：章节层级、表格行列、公式符号、图表位置结构化落盘。
- **核得回**：每张幻灯片、每条结论都带 `quote`（原文逐字片段）+ `locator`（页码/区块 ID），读不出的如实标注，不猜不补。

## 2. 运行契约（务必遵守）

| 项 | 值 |
|---|---|
| 输入目录 | `/input`（含 `case.json`、`task.md`、论文 PDF/附件） |
| 工作目录 | `/app/work`（中间产物 `parsed.json`） |
| 输出目录 | `/app/output`（最终产物，必须在此） |
| 结果文件 | `/app/output/result.json`（16 个顶层字段，缺值给 `null`/`[]`/`{}`） |
| 状态取值 | `ok` / `partial` / `failed` |
| 退出码 | 脚本永远退出 0；错误写入 `result.errors`，不让 harness 因非零退出丢结果 |
| 预算 | 64 次模型请求 / 900 秒 / 单次响应 ≤16384 tokens（含推理）。**约 780 秒必须立即 finalize** |
| 资源约束 | ≤50 个 UTF-8 文本文件、单文件 ≤500KB、总计 ≤2MB、无符号链接 |

`case_id` 与 `output_contract` 从 `/input/case.json` 递归定位，**不得写死**。`output_contract` 原样透传（键名、层级、未知键全部保留），找不到就写 `{}`。

## 3. 输入发现（不假设键名）

1. 递归列 `/input` 下的文件，按文件头 `%PDF-` 识别 PDF，按文件名识别 `case.json`、`task.md`。
2. 读 `case.json`：递归找 `case_id`（`case_id`/`caseId`/`id`/`task_id`）和 `output_contract`（`output_contract`/`output_schema`/`contract`）。
3. 读 `task.md`：确认本题具体任务、输出字段与证据范围，**以题面实际要求为准**，不要照搬示例字段。
4. 读 PDF：用 `pdfinfo` 拿页数，用 `pdftotext -f 1 -l 3 <pdf> -` 探测是否有文字层。

## 4. 输出契约

### 4.1 主产物（都放在 `/app/output`）

| 文件 | 格式 | 说明 |
|---|---|---|
| `result.json` | JSON | 16 字段结构化结果（见 4.2） |
| `deck.pptx` | 可编辑 PPTX | 论文组织成的幻灯片 |
| `deck.pdf` + `previews/page-*.png` | 渲染预览 | PPTX 的真实渲染，供检查 |
| `extracted_text.md` | Markdown | 带页码标记的全文（证据匹配语料） |
| `outline.md` | Markdown | 逐页/逐节提纲（含页级证据） |
| `speaker_notes.md` | Markdown | 讲稿/备注全文 |
| `evidence_map.json` | JSON | 幻灯片 → 页码/区块映射 |

### 4.2 result.json 16 字段

```json
{
  "case_id": "from_input",
  "status": "ok|partial|failed",
  "summary": "",
  "artifacts": [{"path": "/app/output/...", "kind": ""}],
  "primary_result": {"document": {"title": "", "pages": 0, "sections": [], "tables": [], "figures": [], "formulas": [], "references": []}, "extracted_text_path": "/app/output/extracted_text.md"},
  "evidence": [{"id": "E1", "claim": "", "quote": "", "locator": "paper p.N [Uxxxx]", "kind": "direct", "verified": true}],
  "sources": [{"id": "S1", "file": "", "kind": "pdf", "pages": 0, "readable": true, "text_layer": true}],
  "coverage": {"required": 8, "done": 6, "rate": 0.75, "missing": [], "notes": ""},
  "verification": {"checks": [], "fail_count": 0, "warn_count": 0},
  "uncertainty": [], "pending_human": [], "errors": [], "degradations": [], "changes": [],
  "runtime": {"requests": 0, "elapsed_seconds": 0, "script_versions": {}},
  "output_contract": {}
}
```

`status` 由覆盖率和校验计数决定，不靠自评：主产物齐备 + 关键证据全部可核验 + 覆盖率达标才 `ok`；有缺口/降级/待人工项为 `partial`；无输入或解析全失败为 `failed`（`failed` 也要写满 16 字段）。

## 5. 核心流程（按顺序执行，脚本在 `<skill_root>/scripts/`）

```bash
# 0) 定位 skill 根目录（本 SKILL.md 所在目录）
SKILL_ROOT="$(dirname "$(find / -name SKILL.md -path '*weekly-skill*' 2>/dev/null | head -1)")"
# 或已知运行时安装路径：/skills/weekly-skill

# 1) 盘点 + 解析（第一步就落盘 partial result.json）
python3 "$SKILL_ROOT/scripts/parse_pdf.py" run

# 2) 生成可编辑 PPTX + 提纲 + 讲稿 + 证据映射
python3 "$SKILL_ROOT/scripts/build_deck.py"

# 3) 渲染 PPTX → PDF → PNG 预览
python3 "$SKILL_ROOT/scripts/render_preview.py"

# 4) 组装最终 result.json（16 字段 + 校验 + 覆盖率）
python3 "$SKILL_ROOT/scripts/finalize.py"
```

每步结束后检查 stdout 的 JSON 摘要；失败不中断后续步骤（每个脚本内部已捕获异常并落盘）。

**Agent 的额外职责**（在脚本之间可做，但不得编造）：

- 读 `case.json`/`task.md`，若题目指定了额外输出字段，把真实值填进 `primary_result` 或指定主产物文件。
- 若脚本报告"无文字层"（扫描件），按第 11 节走 OCR 分支。
- 若题目要求"问答/翻译/知识结构化"而不仅是解析，在 `extracted_text.md` 的基础上完成，并把结论逐条回填 `evidence`（带 quote + locator）。
- 可对幻灯片正文做"一句话要点"提炼，但**必须保留** `speaker_notes` 中的原文与页码证据，不得用概括替换证据。

## 6. 质量规则映射表（题面评分要素 → 字段/检查/产物）

| 题面要求 | result.json 字段 | 机械检查 | 产物位置 |
|---|---|---|---|
| 提得全（正文/图注/脚注/参考文献） | `primary_result.document` + `sources` | 结构是否齐备 | `extracted_text.md`、`outline.md` |
| 排得对（多栏阅读顺序） | `primary_result.document.sections[].page` | 栏序是否按列主序 | `parsed.json.blocks[].col` |
| 拆得清（章节/表格/公式） | `primary_result.document.tables/formulas` | 表格行列/公式条目数 | `deck.pptx` 表格页/公式页 |
| 核得回（页级证据） | `evidence[].quote/locator` | quote 逐字匹配 | `evidence_map.json`、`speaker_notes.md` |
| 可靠体验（异常处理） | `errors`/`degradations`/`status` | 负例仍产合法 result.json | `result.json` |
| 生态贡献（可复用） | `artifacts` + 本 SKILL.md | 脚本 `--help` 可运行 | `scripts/`、`README.md` |

## 7. 证据与定位规范（逐字可核验）

1. `quote` 是从原文复制的**连续逐字片段**（建议 8–160 字符），不改标点、不补省略、不翻译、不拼接。
2. `locator` 语法：`paper p.3-4 [U0012]`（页码 + 区块 ID）、`paper p.2 table T02-1`、`paper p.5 figure F05-1`、`paper p.8 [R08-1]`。
3. 匹配算法：NFKC 归一 + 空白折叠 + 断词连字符复原 + 引号/破折号统一后再子串匹配；匹配失败即 `verified: false` 并进 `uncertainty`，**不改写原文凑通过**。
4. 找不到证据就进 `pending_human`（写可执行的下一步），不把不可核验内容写成事实。

## 8. 冲突/缺口/人工确认

- 材料缺失、版本冲突、页码错乱、表格跨页断裂 → 分别记入 `uncertainty` 或 `pending_human`，附可执行下一步。
- "未报告 / 无法判断 / 拒答"必须写明原因，不与"事实结论"混写。
- 不覆盖输入、不删除已写内容；改动记入 `changes`。

## 9. 预算与断点

- 第 1 步 `parse_pdf.py run` 立即写 partial `result.json`；此后每完成一个脚本重写一次。
- 接近 **780 秒** 或 **56 次请求** 时停止扩写，直接跑 `finalize.py` 收尾；已写内容不删。
- 单页/单文件失败要隔离（`parse_pdf.py` 已逐页 try/except），不影响全局。

## 10. 幂等与可复现

- 重复运行输出一致；时间戳只进 `runtime`，不进主产物。
- 脚本根目录由 `__file__` 自算，不依赖 CWD；支持 `/skills/weekly-skill`、`/app/submission`、`<root>/submission`、`<root>` 多种布局回退。

## 11. 异常与降级分支表

| 情况 | 识别 | 处置 |
|---|---|---|
| 无输入 PDF | 盘点无 PDF | `status=failed`，写 `errors`，仍写满 16 字段 |
| 无文字层（扫描件） | `text_layer=false` 或 blocks 为空 | `pdftoppm -png -r 300` 渲染 + `tesseract` OCR（发现 `tesseract` 后按语言包 `-l eng+chi_sim` 尝试），OCR 结果写回 `extracted_text.md` 并标注 `ocr=true` |
| 加密 PDF | `pdfinfo` 报 encrypted 或提取为空 | 记 `degradations`，进 `pending_human` 提示需要口令 |
| 多栏版式 | 脚本自动检测列数 | 无需干预；人工抽查 `parsed.json` 中 `col` 赋值 |
| 依赖缺失 | 脚本 import 失败 | 自动降级（fitz→pdfplumber→pdftotext；python-pptx→最小 OOXML），记 `degradations` |
| 输出不可写 | `write_result` 失败 | 尝试 `result.json.tmp`→替换；仍失败写 stderr 并退出 0 |
| 预算耗尽 | 时间/请求超限 | 无条件 `finalize.py` 收尾 |

## 12. 自检清单（提交前逐项勾选）

- [ ] `SKILL.md` 存在，frontmatter 合法（`name`/`description`/`version`），`name` kebab-case 且与目录一致
- [ ] SKILL.md 提到的每个相对路径在包内真实存在
- [ ] `python3 scripts/parse_pdf.py --help`、`build_deck.py --help`、`render_preview.py`、`finalize.py` 退出码 0
- [ ] 空输入 / 坏 JSON / 缺 PDF 仍产出合法 `result.json`
- [ ] `result.json` 16 字段齐全，`output_contract` 与输入逐键一致
- [ ] 抽查 3 条 `quote` 能在 `extracted_text.md` 逐字检索到
- [ ] `status` 与覆盖率/校验结果自洽
- [ ] 无 `__pycache__`、无大二进制、无凭据
- [ ] 文件数 ≤50、单文件 ≤500KB、总计 ≤2MB

## 13. 最小调用示例（示例，非正式案例结果）

输入 `/input`：

```text
/input/case.json   -> {"case_id": "demo-case-01", "output_contract": {"notes_path": "notes.md"}}
/input/task.md     -> 解析论文并生成可编辑 PPT
/input/paper.pdf   -> 一份 8 页双栏论文
```

执行：

```bash
SKILL_ROOT=/skills/weekly-skill
python3 "$SKILL_ROOT/scripts/parse_pdf.py" run
python3 "$SKILL_ROOT/scripts/build_deck.py"
python3 "$SKILL_ROOT/scripts/render_preview.py"
python3 "$SKILL_ROOT/scripts/finalize.py"
```

示意 `result.json`（节选，仅示例）：

```json
{
  "case_id": "demo-case-01",
  "status": "ok",
  "summary": "示例：解析 8 页双栏论文，产出 12 页幻灯片，证据 18/18 条逐字匹配。",
  "primary_result": {"document": {"title": "示例标题", "pages": 8, "sections": [{"heading": "Introduction", "page": 1, "level": 1}], "tables": [], "figures": [], "formulas": [], "references": []}, "extracted_text_path": "/app/output/extracted_text.md"},
  "output_contract": {"notes_path": "notes.md"}
}
```

> 以上为**示意**，不冒充任何正式案例结果；正式案例由平台提供并运行。

## 14. 文件清单与维护

| 文件 | 被谁调用 | 版本 | 说明 |
|---|---|---|---|
| `SKILL.md` | harness 加载入口 | 1.0.0 | 本文件，唯一入口 |
| `scripts/_common.py` | 其余脚本 | 1.0 | 路径/result 骨架/工具发现/输入发现 |
| `scripts/parse_pdf.py` | 流程第 1 步 | 1.0 | 盘点 + 多栏解析 + 结构分类 |
| `scripts/build_deck.py` | 流程第 2 步 | 1.0 | 生成 PPTX/提纲/讲稿/证据映射 |
| `scripts/render_preview.py` | 流程第 3 步 | 1.0 | PPTX→PDF→PNG 渲染 |
| `scripts/finalize.py` | 流程第 4 步 | 1.0 | 校验 + 组装 result.json |
| `README.md` | 人/Agent 阅读 | 1.0 | 使用与方法说明 |
| `resources/result_schema.json` | 参考 | 1.0 | 16 字段机器可读约束 |
| `examples/example_result.json` | 参考 | 1.0 | 最小示例（标注为示例） |
| `GITHUB_URL.txt` | 评审 | — | 第 1 行为作品 GitHub 公开链接 |

已知限制：公式与特殊字符按文本/结构识别，不做语义理解；扫描件依赖运行时 `tesseract` 与语言包；跨页表格合并为"逐页表格 + 页号"，如需跨页合并需 agent 二次处理。
