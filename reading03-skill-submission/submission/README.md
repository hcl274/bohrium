# paper-to-bilingual-translation

把科研文献翻译成**段落级、可核验的中英双语对照**，附带可执行的术语表与机械核验，而不是只给大意或改写。

## 解决什么问题

文献翻译的三个高频失败点，本 Skill 用脚本固定住：

1. **术语漂移**——术语表只写在提示里没人执行。本 Skill 用 `glossary.json` 锁定译名，`verify.py` 回扫全篇一致率；没有原文逐字 quote 的术语不允许锁定。
2. **跨页跨栏漏段/重复**——`prepare.py` 先做双栏阅读顺序纠正，再按稳定段号切段；段号与原文一一对应，逐段续译不跳号。
3. **润色改数字/单位/引用**——`verify.py` 机械比对数字、单位、`[N]` 引用、`(N)` 公式编号，只回改 fail 的段。

## 方法（7 步）

盘点 → 切段 → 建术语表 → 逐段翻译 → 机械核验 → 只改 fail 的段 → 无条件 finalize。

## 依赖

纯 Python 标准库即可运行；按优先级回退使用以下任一 PDF 工具：

1. PyMuPDF（`pymupdf` 或 `fitz`，推荐）
2. pdfplumber
3. `pdftotext -bbox`（poppler-utils）
4. `pdftotext -layout`（poppler-utils）

TXT/DOCX/HTML 或 `case.json` 内嵌正文无需任何第三方依赖。

## 输入 / 输出

- 输入：`/input/`（`case.json`、`task.md`、PDF/TXT/DOCX/HTML 或内嵌正文）
- 输出：`/app/output/`
  - `translation.md` 段号 + 原文 + 译文 + 依据
  - `glossary.csv` 源词 / 锁定译名 / 状态 / 证据段号 / quote
  - `extracted_text.md` 带段号与页码的原文底稿（证据匹配用）
  - `result.json` 16 字段结构化结果

## 调用步骤

```bash
python3 scripts/prepare.py run     # 解析+切段+术语候选
# 读 work/segments.json、work/glossary.json；锁定术语、逐段翻译，写 work/translations.json
python3 scripts/verify.py          # 机械核验
# 仅重译 failed_segments，再 verify
python3 scripts/finalize.py        # 无条件组装最终产物
```

## 限制与失败处理

- 数学公式、参考文献条目保留原文（学界惯例），不作为"未译"；正文中的 `[N]`、`(N)` 编号必须保留。
- 扫描件无文本层：标注 `degradation` 且 `status=failed`，不伪造文字。
- PDF 加密/损坏致 0 段：仍输出合法 16 字段 `result.json`，`errors[]` 记录原因。
- 三种占位符语义分开：`〔待补:…〕`未做 / `〔未译:…〕`脚本占位 / `〔存疑:…〕`已译不确信。

## 示例

见 `examples/example_result.json`（示意输出，仅说明接口，非正式案例结果）。
