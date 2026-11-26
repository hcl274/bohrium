# 使用与方法说明

## 目标与接口

本技能把科研 PDF 转成可定位的结构化材料，支持后续问答、翻译、参数提取和核验。主要改进点是：在保留原始文本的同时，依据空白栏缝与跨栏区块还原阅读顺序；每条文字、每个表格单元格都有页码、坐标与稳定的本次运行标识。最终答案仍由本案任务及 `output_contract` 决定。

输入是实际 PDF 路径。输出目录由调用方明确指定。运行过程不联网、不上传材料、不调用外部模型服务、不改写输入 PDF。没有注册账号、API 令牌或个人目录依赖。

## 环境与依赖

- Python 3.10 或更新版本；除 PDF 后端外，核心逻辑只用标准库。
- 推荐 PyMuPDF，开发验证版本为 **1.28.2**；常用文字提取接口兼容较早版本，`find_tables` 不存在时会记录表格功能缺失。安装：`python -m pip install -r requirements.txt`。在平台优先使用已有环境，不默认安装依赖。
- 文字降级后端：Poppler 的 `pdftotext`。使用 `-bbox-layout -enc UTF-8` 保留词坐标。测试环境二进制版本会写在提交包外的验证记录中。此后端不提供字体、合并表格网格或真实图像枚举，相关内容要对照页面补核。
- 页面预览：PyMuPDF 渲染，或 Poppler `pdftoppm`。不要求 Office 软件。
- 可选 OCR：Tesseract 4/5 命令行程序与对应语言数据，默认请求 `eng+chi_sim`。可用 `tesseract --list-langs` 查看。只启用实际存在的请求语言，并记录缺失语言。Linux 可由环境管理员准备 `tesseract-ocr`、`tesseract-ocr-eng` 和 `tesseract-ocr-chi-sim`；程序不会执行系统安装。
- 若系统已有工具但不在 PATH，可在当前运行进程的 PATH 中加入清单声明的工具目录。不得据此编造“软件已安装”。

推荐版本用于复现；其他版本的输出可能存在字体、表格或坐标差异。未捆绑第三方程序、语言模型、字体或论文；依赖遵循各自许可证。维护时应先运行回归样例，再调整版本。

## 最小可运行示例

以下是**自建示例**，用于展示输入、调用与输出，不是平台正式案例，也不是预制的参赛答案。

```bash
python scripts/make_example.py --out /app/work/example.pdf
python scripts/parse_pdf.py /app/work/example.pdf --out /app/output/example --ocr off --render all
python scripts/case_tools.py search /app/output/example --query "Reference"
python scripts/case_tools.py audit /app/output/example
```

在 Windows PowerShell 中可以把 `/app/work` 和 `/app/output` 换成当前工作目录内的路径：

```powershell
python scripts/make_example.py --out ./example.pdf
python scripts/parse_pdf.py ./example.pdf --out ./example-output --ocr off --render all
python scripts/case_tools.py search ./example-output --query "Reference"
```

预期可观察行为：输入 PDF 的内部文字写入顺序是右栏在先；`document.md` 的阅读正文先列出 `LEFT 01` 到 `LEFT 08`，再列出右栏。表格得到两行两列，`Reference` 与 `42` 各有单元格坐标，`tables/` 下有可继续编辑的 HTML 表格。

示意输出（仅展示字段形态，标识和坐标以实际运行文件为准）：

```json
{
  "status": "success",
  "backend": {"name": "pymupdf", "version": "1.28.2"},
  "coverage": {
    "requested_pages_completed": true,
    "complete_document": true,
    "table_count": 1
  }
}
```

这段示意不是完整 `document.json`，也不是任何正式案例要求的 `result.json`。

## 参数与运行预算

```text
parse_pdf.py INPUT.pdf --out DIRECTORY
  --pages 1-3,5           物理页码从 1 开始，保留真实页号
  --backend auto         auto / pymupdf / poppler
  --ocr auto             auto / off / force
  --lang eng+chi_sim      请求 OCR 语言；不自动下载语言包
  --dpi 240              OCR 渲染分辨率，允许 72..600
  --render uncertain     uncertain / all / none
  --preview-dpi 120      预览分辨率，允许 36..300
  --preview-limit 24     最多保存所选前 24 页的普通预览
  --budget 180           解析时间预算（秒）
  --ocr-timeout 45       每页 OCR 超时上限（秒）
  --password-env PDF_PASSWORD   加密 PDF 密码的环境变量名
```

`uncertain` 会为图像、表格、较复杂文字页及有复核项的页面保存预览；简单页可能不生成。OCR 输入图独立保留在 `ocr/`。预览只用于检查，正文、文字锚点和表格仍是结构化数据。

预算在每页开始前检查，外部进程有超时。正在执行的单次原生库调用不能被本进程强制打断，因此不能把 `--budget` 当成严格的整案硬超时。必要时由调用环境再设置进程总时限。使用 PyMuPDF 时每页保存 `checkpoint.json`；整案程序应尽早保存阶段性的契约结果。

## 方法与可复核边界

### 多栏与跨栏

提取每条文字线的矩形、字号、字体与原生块编号。根据窄文字行的横向占用寻找低穿越率的垂直空隙；递归处理三栏等布局。跨越栏缝的元素构成垂直分段屏障，同一分段内按左栏到右栏、每栏从上到下组织。同一跨栏段落的短末行保留在原段落。

相较直接按 PDF 内部写入顺序读文，几何分区能处理“右栏先写入”的文件；相较全页按 y 坐标排序，可减少左右栏逐行交错。这是需要实际文档验证的启发式，不声称覆盖任意版式。边注、长公式、跨栏图片和重叠浮动元素可能形成错误分区，应对照预览检查并在最终结果记录修订。

不自动拼接断行连字符，不执行 Unicode NFKC 正规化，不改写数字、数学符号或否定词。几何空格插入仅影响 `text`；`raw_text` 和 `spans` 保留原样，变更另有记录。搜索时的归一化不改写输出原文。

### 表格

PyMuPDF 的线框表格检测给出网格与单元格。按实际矩形边界构建行列坐标轴，以矩形跨越的网格数确定 `rowspan/colspan`。HTML 保留合并关系，JSON 保留每格坐标、原始抽取行及空缺位置。表头名称是后端推断，`header_inference_verified` 默认为 `false`。

表格行中的文字锚点保留在 `anchors.jsonl`，正文中用表格结构替代重复展平。跨页候选需要同时满足相邻物理页、页底/页顶位置、相同列数和重复首行或同表号线索；不会自动删除重复表头或合并数值。无框表格、嵌套表格、单元格内图像、斜线表头及复杂跨页结构需图像复核。

### 扫描与混合页面

少量原生文字配合大面积图像、缺失文字层或替换字符会触发 OCR 检查。Tesseract TSV 词框按渲染尺寸换算到 PDF point。已有可靠原生文字区域优先使用原生层，避免把 OCR 重复追加到正文；被 OCR 替代的损坏原生行仍留作锚点。

OCR 不承诺逐字正确，默认形成复核项。混合 PDF 中小图内文字可能没有自动触发；可对相关页使用 `--ocr force`，但仍应检查原始图像。复杂扫描表格不会因 OCR 得到文字就自动声称网格完整。

### 公式、题注、章节与元数据

章节层级结合编号与字体特征产生候选；没有可靠编号的层级为 `null`。首页标题、作者、单位需由执行任务的 Agent 结合实际首页与元数据核对。PDF 的内嵌 metadata 可能过时，不能直接当作页面内容的真值。

数学字体、公式编号和符号组合用于定位公式候选；不推导科学含义。原生 spans 中的字体标记、字号与基线可辅助上下标核验；OCR 不提供等价的字体证据。题注与近邻嵌入图像的连接也属于候选，矢量图可能只有题注而没有图像矩形。

### 异常状态

- `success`：请求页完成结构提取且没有脚本发现的强制复核项；不等于问答或全文校对完成。
- `partial`：已有结果但存在 OCR、缺字、预算、部分工具失败等未解决项。
- `blocked`：文件缺失、密码不可用或没有可用文字后端等，无法开始有效提取。
- `failed`：损坏文件、非法页范围等导致运行失败。

退出码 `0` 可能对应有复核项的 `partial`；调用方必须同时读取 JSON 状态。阻塞或异常退出码为 `2`。不同输入不能复用已有解析目录覆盖结果；相同输入可重跑。重跑后的权威文件是新 `document.json`，不要把目录中旧的未引用图片当作本次证据。

## 自查与复用

```bash
python scripts/case_tools.py audit /app/output/paper
python scripts/case_tools.py check-result --case /input/case.json --result /app/output/result.json
python scripts/case_tools.py check-package .
```

`audit` 检查锚点唯一性、坐标范围、文字引用和表格文件是否存在。`check-result` 检查常见契约结构，不验证语义或所有 JSON Schema 关键字。`check-package` 检查 UTF-8 文本、文件数量、大小、符号链接和根目录 frontmatter。

版本号为 `1.0.0`。接口变更时同步修改 `SCHEMA.md` 和命令示例；报告缺陷时提供脱敏最小样例、后端版本、页码、锚点和预期顺序，不附带令牌或个人私密材料。

## 依赖原始文档

- PyMuPDF 文本提取：https://pymupdf.readthedocs.io/en/latest/recipes-text.html
- PyMuPDF 页面与表格接口：https://pymupdf.readthedocs.io/en/latest/page.html
- Tesseract 命令、语言与 TSV：https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html
- Poppler pdftotext：https://manpages.debian.org/bookworm/poppler-utils/pdftotext.1.en.html

本包只提交可复用指令与脚本。正式案例结果与平台对照评分由评测系统运行产生。当前没有公开仓库链接，不申报相应附加分。
