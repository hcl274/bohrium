"""Geometric reading order and conservative paragraph grouping. Standard library only."""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter


def median(values, default=10.0):
    values = [float(v) for v in values if v is not None and math.isfinite(float(v)) and float(v) > 0]
    return statistics.median(values) if values else default


def bbox_union(items):
    boxes = [v["bbox"] if isinstance(v, dict) else v for v in items]
    return [round(min(b[0] for b in boxes), 3), round(min(b[1] for b in boxes), 3),
            round(max(b[2] for b in boxes), 3), round(max(b[3] for b in boxes), 3)]


def overlap(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
    return intersection / area


def _gutter(items):
    """Find a low-crossing vertical lane. Spanning lines are handled separately."""
    if len(items) < 6:
        return None
    left, _, right, _ = bbox_union(items)
    width = right - left
    if width < 100:
        return None
    narrow = [i for i in items if i["bbox"][2] - i["bbox"][0] <= 0.72 * width]
    if len(narrow) < 6:
        return None
    step = max(1.0, width / 240)
    runs = []
    start = None
    for k in range(25, 216):
        x = left + k * width / 240
        a = sum(i["bbox"][2] < x - 1 for i in narrow)
        b = sum(i["bbox"][0] > x + 1 for i in narrow)
        crossing = len(narrow) - a - b
        good = a >= 3 and b >= 3 and crossing <= max(0, int(0.08 * len(narrow)))
        if good and start is None:
            start = x
        if not good and start is not None:
            runs.append((start, x - step))
            start = None
    if start is not None:
        runs.append((start, left + 215 * width / 240))
    runs = [r for r in runs if r[1] - r[0] >= max(7, 0.018 * width)]
    if not runs:
        return None
    best = max(runs, key=lambda r: (r[1] - r[0], -abs((r[0] + r[1]) / 2 - (left + right) / 2)))
    x = (best[0] + best[1]) / 2
    crossing = [i for i in items if i["bbox"][0] < x < i["bbox"][2]]
    if len(crossing) > max(5, len(items) * 0.35):
        return None
    return x


def reading_order(items, depth=0):
    """Column-major order inside zones separated by actual spanning elements."""
    if not items:
        return [], []
    cut = _gutter(items) if depth < 4 else None
    if cut is None:
        vertical = sorted(items, key=lambda i: (i["bbox"][1], i["bbox"][0], i["id"]))
        # Fragments on one visual row can have slightly different font bounding boxes.
        # Resolve those by x, rather than moving an email/symbol before its left prefix.
        rows = []
        tolerance = min(2.5, median([i["bbox"][3]-i["bbox"][1] for i in items])*0.2)
        for item in vertical:
            if rows and abs(item["bbox"][1]-rows[-1][0]["bbox"][1]) <= tolerance:
                rows[-1].append(item)
            else:
                rows.append([item])
        return [item for row in rows for item in sorted(row,key=lambda i:(i["bbox"][0],i["id"]))], []
    spans = [i for i in items if i["bbox"][0] < cut < i["bbox"][2]]
    ordinary = [i for i in items if i not in spans]
    # Keep the short last line of a spanning paragraph with that paragraph.
    span_groups = {i.get("origin_block") for i in spans if i.get("origin_block") is not None}
    extra = [i for i in ordinary if i.get("origin_block") in span_groups and
             any(abs(i["bbox"][0] - s["bbox"][0]) < 8 and i.get("origin_block") == s.get("origin_block") for s in spans)]
    spans += extra
    ordinary = [i for i in ordinary if i not in extra]
    spans.sort(key=lambda i: (i["bbox"][1], i["bbox"][0]))
    ordered = []
    cuts = [round(cut, 3)]

    def emit_zone(zone):
        a = [i for i in zone if (i["bbox"][0] + i["bbox"][2]) / 2 <= cut]
        b = [i for i in zone if i not in a]
        for part in (a, b):
            seq, subcuts = reading_order(part, depth + 1)
            ordered.extend(seq)
            cuts.extend(subcuts)

    rest = ordinary
    for span in spans:
        mid = (span["bbox"][1] + span["bbox"][3]) / 2
        before = [i for i in rest if (i["bbox"][1] + i["bbox"][3]) / 2 < mid]
        rest = [i for i in rest if i not in before]
        emit_zone(before)
        ordered.append(span)
    emit_zone(rest)
    return ordered, sorted(set(cuts))


def mark_margins(pages):
    signatures = Counter()
    occurrences = {}
    for page in pages:
        for line in page["lines"]:
            b = line["bbox"]
            zone = "header" if b[3] < page["height"] * 0.085 else "footer" if b[1] > page["height"] * 0.915 else None
            if not zone:
                continue
            text = re.sub(r"\d+", "#", line["text"].strip().lower())
            text = re.sub(r"\s+", " ", text)
            if len(text) <= 180:
                key = (zone, text)
                occurrences.setdefault(key, set()).add(page["page_number"])
    for key, nums in occurrences.items():
        signatures[key] = len(nums)
    threshold = max(2, math.ceil(len(pages) * 0.5))
    for page in pages:
        for line in page["lines"]:
            b = line["bbox"]
            zone = "header" if b[3] < page["height"] * 0.085 else "footer" if b[1] > page["height"] * 0.915 else None
            if not zone:
                continue
            key = (zone, re.sub(r"\s+", " ", re.sub(r"\d+", "#", line["text"].strip().lower())))
            page_only = zone == "footer" and re.fullmatch(r"[-–—]?\s*(?:\d+|[ivxlcdm]+)\s*[-–—]?", line["text"].strip(), re.I)
            if page_only or signatures[key] >= threshold:
                line["role"] = zone
                line["role_basis"] = "page_number_at_margin" if page_only else "repeated_margin_signature"


CAPTION = re.compile(r"^(?P<kind>table|tab\.|figure|fig\.|图|表)\s*(?P<number>[A-Za-z]?\d+(?:[.\-]\d+)*)(?=$|[^\d])", re.I)
SECTION = re.compile(r"^(\d+(?:\.\d+){0,4})[.、]?\s+\S")
NAMED = {"abstract", "references", "bibliography", "acknowledgments", "acknowledgements", "appendix", "摘要", "关键词", "参考文献", "致谢", "附录"}


def classify_line(line, body_size):
    if line.get("role") in {"header", "footer", "table_text"}:
        return line["role"]
    text = line["text"].strip()
    if CAPTION.match(text):
        return "caption"
    size = line.get("font_size") or body_size
    bold = any(s.get("flags", 0) & 16 for s in line.get("spans", []))
    heading = len(text) <= 180 and (text.lower().rstrip(":：.") in NAMED or
               size >= body_size * 1.4 or (size >= body_size * 1.16 and bold) or
               (SECTION.match(text) and bold and size >= body_size * 0.97))
    if heading:
        return "heading"
    math_chars = sum(len(s.get("text","")) for s in line.get("spans", [])
                     if re.search(r"symbol|cmmi|cmsy|msam|stix.*math|math", s.get("font", ""), re.I))
    math_font = len(text) <= 180 and math_chars / max(1,len(text)) >= 0.35
    numbered_formula = re.search(r"[（(]\d+[a-z]?[）)]\s*$", text) and re.search(r"[=∑∫≤≥≈∂√]|\b(?:sin|cos|log|exp)\b", text)
    if math_font or numbered_formula:
        return "equation_candidate"
    return "paragraph"


def make_blocks(page, body_size):
    lines = [l for l in page["lines"] if l.get("role") not in {"header", "footer", "table_text", "superseded_native"}]
    ordered, cuts = reading_order(lines)
    page["column_cuts"] = cuts
    blocks = []
    for line in ordered:
        role = classify_line(line, body_size)
        line["role"] = role
        previous = blocks[-1] if blocks else None
        merge = False
        caption_continuation = False
        if previous and previous["kind"] == "caption" and role == "paragraph":
            last = previous["lines"][-1]
            same_caption_style = abs((line.get("font_size") or body_size)-(last.get("font_size") or body_size)) < body_size*0.12
            if ((line.get("origin_block") == last.get("origin_block") or same_caption_style) and
                    abs(line["bbox"][0]-last["bbox"][0]) < 18 and
                    -body_size*0.45 <= line["bbox"][1]-last["bbox"][3] <= body_size*0.5):
                role = line["role"] = "caption"
                caption_continuation = True
        if previous and previous["kind"] == role and role not in {"heading", "caption"}:
            last = previous["lines"][-1]
            gap = line["bbox"][1] - last["bbox"][3]
            same_native = line.get("origin_block") == last.get("origin_block")
            aligned = abs(line["bbox"][0] - last["bbox"][0]) < max(18, 1.7 * body_size)
            merge = same_native and aligned and -body_size*0.45 <= gap <= max(7, body_size * 0.95)
        merge = merge or caption_continuation
        if merge:
            previous["lines"].append(line)
        else:
            blocks.append({"kind": role, "lines": [line]})
    for index, block in enumerate(blocks, 1):
        block["id"] = f"p{page['page_number']:04d}-b{index:04d}"
        block["page_number"] = page["page_number"]
        block["bbox"] = bbox_union(block["lines"])
        block["text"] = "\n".join(l["text"] for l in block["lines"])
        block["raw_text"] = "\n".join(l["raw_text"] for l in block["lines"])
        block["line_ids"] = [l["id"] for l in block.pop("lines")]
        if block["kind"] == "heading":
            match = SECTION.match(block["text"])
            block["level"] = min(6, len(match.group(1).split("."))) if match else None
        if block["kind"] == "caption":
            match = CAPTION.match(block["text"])
            block["caption_kind"] = "table" if match.group("kind").lower().startswith("tab") or match.group("kind") == "表" else "figure"
            block["label"] = match.group("number")
    page["blocks"] = blocks
    return blocks
