---
name: paper-to-bilingual-translation
description: 将科研文献（PDF/TXT/DOCX/HTML 或 case.json 内嵌正文）翻译成段落级、可核验的中英双语对照，含术语表锁定、长文分段续译与机械核验。当任务涉及文献翻译、论文翻译、术语一致性、双语对照、译文核验、段号对齐时使用。触发词：翻译、translate、文献翻译、双语对照、术语表、glossary、paper translation。
version: 1.0.0
license: MIT
---

# 文献翻译 Skill：段落级双语对照 + 术语表 + 机械核验

把一篇科研文献译成**可复核的双语对照结果**，而不是给大意或改写。四个硬约束：

- **术语稳**：同一术语在标题/摘要/正文/图注/结论中始终用同一译名；术语表能被脚本真正执行，而不是只写在提示里。
- **段落全**：跨页、跨栏、跨章节不漏段、不重复、不错位；段号与原文一一对应。
- **关系准**：指代、否定与限定、因果与让步、数字单位、公式编号与文献引用标记与原文一致。
- **可核验**：每段译文能指回原文具体段号并说明译法依据；译不准的地方明确标注，不粉饰。

## 运行契约（Harbor）

- 输入目录 `/input`：`case.json`（可能带 `output_contract` 与 `case_id`）、`task.md`、科研材料（PDF 或 TXT/DOCX/HTML，或 case.json 内嵌正文）。
- 工作目录 `/app/work`：中间产物（segments.json、glossary.json、translations.json、verification.json）。
- 输出目录 `/app/output`：`translation.md`、`glossary.csv`、`extracted_text.md`、`result.json`。
- `result.json` 必须包含 16 个字段且原样回填 `output_contract` 与 `case_id`（脚本已处理）。
- 预算：64 次模型请求 / 900 秒 / 单次响应 16384 token。**每批翻译尽早落盘**，不要最后一次性写。

## 核心流程（7 步）

1. **盘点**：`python3 /skills/weekly-skill/scripts/prepare.py run`。它发现材料、纠正双栏阅读顺序、切段并抽术语候选，产出 `work/segments.json` 与 `work/glossary.json`，并写早期 partial `result.json`。
2. **读段与术语候选**：读 `work/segments.json`（`segments[]`：`id` 形如 `S0001-001`、`page`、`type`、`text`）与 `work/glossary.json`（`glossary[]`：`source`、`status=candidate`）。
3. **锁定术语表**：为高频、领域关键、缩写词定译名。**没有原文逐字 quote 的术语不得锁定**——锁定条目必须填 `target`、把 `status` 改为 `locked`，并在 `evidence` 里写 `quote`（从某段 `text` 逐字复制的原文片段）与 `id`/`page`。不锁定的保持 `candidate`。缩写词通常保留原缩写并给中文全称，如 `NLP` → `自然语言处理（NLP）`。
4. **逐段翻译**：按 `segments[]` 顺序翻译，段号与原文一一对应；按类型处理：
   - `heading`：翻译为标题，保留编号（如 `1 Introduction` → `1 引言`）。
   - `text`：完整直译，不压缩、不扩写、不改变逻辑关系与否定范围。
   - `caption`：翻译图注/表题，保留 `Figure N:`/`Table N:` 编号不变。
   - `formula`：**数学公式保留原文**，`zh` 填原文本身，`note` 写 `公式保留原文`。
   - `ref`：**参考文献条目保留原文**（学界惯例），`zh` 填原文本身，`note` 写 `参考文献保留原文`。
   - 每段译文写入 `work/translations.json`，格式 `{"translations":[{"id":"S0001-001","zh":"…","status":"done","note":"","basis":"…"}]}`。`basis` 可选，写"译法依据/存疑说明"。**每批（如 8–12 段）写一次并累加全部已译条目**，不要等全部译完才写。
   - 长文跨页跨栏：段号已由脚本按阅读顺序稳定生成，逐段续译即可，不合并、不跳号。
5. **机械核验**：`python3 /skills/weekly-skill/scripts/verify.py`。它检查数字/单位/引用标记 `[N]`/公式编号 `(N)`/术语一致率，产出 `work/verification.json` 与 `failed_segments`。
6. **只改 fail 的段**：读 `work/verification.json`，仅重译 `failed_segments` 里的段（数字缺失、引用标记缺失、无译文、占位未完成），更新对应 `zh`，再次运行 verify。已通过的段不动。
7. **无条件收尾**：`python3 /skills/weekly-skill/scripts/finalize.py`。无论前序是否全部成功都要执行；它组装 `translation.md`、`glossary.csv`、`result.json`，并诚实判定 `ok/partial/failed`。

## 占位符语义（务必分开，禁止混用）

| 占位符 | 含义 | 计入完成？ |
|---|---|---|
| `〔待补:原因〕` | 尚未翻译 | 否（coverage 缺口） |
| `〔未译:原因〕` | 脚本占位（如公式/引用保留原文应改用 `note` 而非此标记） | 否 |
| `〔存疑:原因〕` | 已译但不确定（如术语歧义、原文残缺） | 是，但记 warning |

公式/参考文献的"保留原文"不是"未译"，应 `status=done` + `note=公式保留原文/参考文献保留原文`，不要用 `〔未译〕` 占位。

## 术语表格式（glossary.json / glossary.csv）

```json
{"glossary": [
  {"source": "reading order", "target": "阅读顺序", "status": "locked",
   "evidence": {"quote": "reading order detection", "id": "S0001-003", "page": 1}},
  {"source": "NLP", "target": "自然语言处理（NLP）", "status": "locked",
   "evidence": {"quote": "NLP", "id": "S0001-001", "page": 1}}
]}
```

`glossary.csv` 列：`source,target,status,evidence_id,quote`。锁定 10–40 条即可，关键是**全篇一致**，不是数量多。

## 依赖与降级

- 首选 PyMuPDF（`pymupdf`/`fitz`）做块提取与列检测；缺失时自动回退 pdfplumber → `pdftotext -bbox` → `pdftotext -layout`。
- 无文本层/扫描件：`prepare.py` 会写 degradation，`result.json.status` 转 `failed` 并注明"需 OCR"，不伪造文字。
- 输入为 TXT/DOCX/HTML 或内嵌正文：按空行切段，逐段翻译即可。
- 若 PDF 加密/损坏导致 0 段：`result.json` 仍输出合法 16 字段，`status=failed`，`errors[]` 记录原因。

## 自检清单（finalize 前）

- [ ] 每个 `segments[]` 的 `id` 都在 `translations.json` 里有对应条目且 `zh` 非空或为合法占位符。
- [ ] `coverage_rate == 1.0` 且 `fail_count == 0` 才允许 `status=ok`，否则 `partial`。
- [ ] 每个 locked 术语都有逐字 `quote`；标题/摘要/正文/图注/结论译名一致。
- [ ] 数字、单位、`[N]` 引用、`(N)` 公式编号未被改/漏。
- [ ] `result.json` 16 字段齐全、`output_contract` 原样回填、`case_id` 已填。

## 最小示例

输入 `case.json`：`{"case_id":"demo"}`；材料为 3 段正文的 PDF/TXT。调用：

```bash
python3 /skills/weekly-skill/scripts/prepare.py run
# 读 work/segments.json，锁定术语并写 work/glossary.json
# 逐段翻译并写 work/translations.json（示意输出见 examples/example_result.json，仅作示例，非正式案例结果）
python3 /skills/weekly-skill/scripts/verify.py
# 重译 failed_segments
python3 /skills/weekly-skill/scripts/finalize.py
```

## 文件清单

- `scripts/prepare.py` 解析 + 切段 + 术语候选
- `scripts/verify.py` 数字/单位/引用/公式/术语机械核验
- `scripts/finalize.py` 组装 translation.md + glossary.csv + result.json
- `scripts/_common.py` 路径、16 字段骨架、输入发现、quote 归一化等共享工具
- `resources/result_schema.json` result.json 字段说明
- `examples/example_result.json` 最小示意输出（标注为示例）
