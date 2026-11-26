# 解析中间格式 traceable-paper/v1

最终案例 `result.json` 遵循案例自己的 `output_contract`；本文件描述解析器的中间材料格式。

## 输出文件

| 文件 | 含义 |
|---|---|
| `document.json` | 本次完整解析状态、输入标识、页、结构、来源和复核项 |
| `document.md` | 按阅读顺序组织的可读正文，含来源注释与表格链接 |
| `anchors.jsonl` | 一行一个文字块、文字行或单元格，可检索和引用 |
| `checkpoint.json` | PyMuPDF 后端阶段快照；尚未完整分块，不当作最终结果 |
| `tables/*.html` | 可编辑的网格表格；保留 rowspan/colspan |
| `previews/*.png` | 实际渲染的页面检查图，路径在页记录中声明 |
| `ocr/*.png` | 实际用于 OCR 的输入图，仅在 OCR 执行时存在 |

## 输入标识、页码与坐标

`sha256` 是输入 PDF 原始字节的 SHA-256，`document_id` 是其前 16 个十六进制字符。多文档引用必须带此标识，避免相同局部页号冲突。`input_path` 是本次实际读取的文件绝对路径。

`page_number` 从 1 开始，`page_index` 从 0 开始，均指原 PDF 的物理页序。`page_label` 是 PDF 内嵌页标签，没有就为 `null`；它不是从页脚 OCR 得到的印刷页码。

`bbox` 为 `[x0,y0,x1,y1]`，左上角原点，横向向右、纵向向下，单位 PDF point（72 point = 1 inch）。PyMuPDF 后端统一在取消页面旋转后的裁剪框坐标系中提取与渲染，记录 `original_rotation`；不会保存修改后的原 PDF。Poppler 使用其输出页坐标系并显式标记。预览像素坐标按实际图像尺寸与页宽高换算。

## 顶层字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | string | `traceable-paper/v1` |
| `parser_version` | string | 脚本版本 |
| `input_path`, `document_id`, `sha256` | string / null | 输入位置与标识；输入读取失败时标识可能为空 |
| `status` | string | success / partial / blocked / failed |
| `backend` | object / null | 实际后端名称与可得版本 |
| `metadata` | object，可缺省 | PDF 内嵌元数据，未当作正文真值验证 |
| `page_count` | integer | 原始总页数 |
| `requested_pages`, `processed_pages` | integer[] | 请求及实际处理的物理页号 |
| `coverage` | object，可缺省 | 完成范围、保留行数、正文块数、表格数和未处理页 |
| `pages` | object[] | 每页文字、结构和渲染路径 |
| `sections` | object[]，可缺省 | 标题候选及来源；不确定的 level 为 null |
| `equations` | object[]，可缺省 | 公式结构候选，未转写 latex 为 null |
| `figures` | object[]，可缺省 | 题注及图像链接候选；没有真实图像定位时 bbox 为 null |
| `table_continuation_candidates` | object[]，可缺省 | 跨页续表连接线索，默认 merged=false |
| `uncertainty` | object[] | code、message、severity、可选 page_number |
| `change_records` | object[] | 调整栏序、插入几何空格、OCR 图层合入等实际操作 |
| `elapsed_seconds` | number | 本次真实脚本运行时间，不等于平台整案耗时 |

## 页、文字与表格

每页包含宽高、原始旋转、坐标系、页标签、`lines`、`blocks`、`tables`、`images` 和 `column_cuts`。`preview_path` 存在时指向实际渲染文件。

行标识如 `p0002-l0014`，OCR 行如 `p0002-o0020`。行含 `raw_text`、可读 `text`、坐标、来源方法、原生块编号与角色。原生 spans 另含 text、bbox、font、size、flags、origin；OCR 行含 words、各词 confidence 与平均置信度，不伪造字体信息。

行角色包括 paragraph、heading、caption、equation_candidate、table_text、header、footer、superseded_native。后两类边缘文字与被替代的原生行仍留在 JSON/锚点中。正文块含 `line_ids`，可以回溯到原行。不同输出视图的块与行都在锚点索引中，检索结果可能来自不同粒度。

表格标识如 `p0002-t001`，单元格标识如 `p0002-t001-r2c1`。JSON 中 `row`、`column` 从 0 开始；标识中的 r/c 从 1 开始。每格都有文本、bbox、rowspan、colspan。`extracted_rows` 是后端原始矩阵，`null` 可能来自合并占位、缺失或无法提取，不能转换成 0。HTML 的 `data-state="unresolved"` 明确标记未解析的网格位置。

`header` 可能是后端推断；只有执行任务时对照原页核对后才能在最终案例答案中确认表头。`caption_id` 和图像连接也是候选。未自动合并续表，后续修改应保存新文件并另记原表 ID、原页号和删除/保留的行。

## 证据引用与契约映射

推荐的引用信息是 `{document_id, source_pdf, page_number, id, bbox, quote}`，但必须映射到本案契约允许的字段名。例如契约要求 `evidence` 就使用该字段；不自行改名为 `sources`。`quote` 应是来源锚点中的短引，不能把推断结果写成原文。

若契约不允许额外字段，运行状态、源文件清单、复核项和改动可以写入单独的 `run_manifest.json`，在任务允许的位置提供路径。必须保留原 `case_id` 的值及类型，不从文件名重新构造案例标识。

检查器支持 JSON Schema 的常见 type、properties、required、additionalProperties、items、enum、const、allOf、anyOf、oneOf、not、局部 $ref、长度与数值边界。不验证 format、完整条件式、外部引用、全部 pattern 方言或语义来源蕴含。检查通过后仍需依据原始 output_contract 逐项核对。
