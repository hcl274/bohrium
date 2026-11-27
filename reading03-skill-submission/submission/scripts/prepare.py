#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prepare.py — 解析文献并把正文切分为可翻译的段落单元 + 抽取术语候选。

读取 /input（case.json、task.md、PDF/TXT/DOCX/HTML 或 case.json 内嵌正文），产出：

  work/segments.json   有序翻译单元 {id, page, type, text}
  work/glossary.json   术语候选 {source, target, status=candidate, evidence}
  output/result.json   早期 partial 骨架（诚实落盘）

切分策略：按 PyMuPDF/pdfplumber/pdftotext 的"块"切段，先做双栏阅读顺序纠正，
再按语义分类（标题/段落/图注/公式/参考文献）。段号 S+页码+序号，与原文一一对应，
跨页跨栏的段落不合并、不丢块，下游翻译时按序号续译即可对齐。

术语候选只做"候选"，不锁定：锁定译名必须带原文逐字 quote（见 verify.py 的
"没有 quote 不锁定"规则）。agent 在候选上补 target 并把 status 置为 locked。

用法：
  python3 prepare.py inventory    # 列出输入与可读状态
  python3 prepare.py prepare      # 解析 + 切段 + 术语候选
  python3 prepare.py run          # inventory + prepare + 写 partial result
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import _common as C


# ---------------------------------------------------------------------------
# Optional third-party imports (guarded)
# ---------------------------------------------------------------------------

def _try_import_fitz():
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            import pymupdf
            return pymupdf
        except Exception:
            pass
        try:
            import fitz
            return fitz
        except Exception:
            return None


def _try_import_pdfplumber():
    try:
        import pdfplumber
        return pdfplumber
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Text extraction ladder
# ---------------------------------------------------------------------------

def page_count(pdf_path: Path) -> int:
    rc, out, _ = C.run_cmd(["pdfinfo", str(pdf_path)], timeout=60)
    if rc == 0:
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        if m:
            return int(m.group(1))
    return 0


def text_layer_present(pdf_path: Path, pages_to_probe: int = 3) -> bool:
    rc, out, _ = C.run_cmd(
        ["pdftotext", "-f", "1", "-l", str(pages_to_probe), str(pdf_path), "-"],
        timeout=120,
    )
    if rc != 0:
        return False
    return len(re.sub(r"\s+", "", out)) >= 40


def _blocks_from_fitz(pdf_path: Path, fitz) -> list:
    blocks = []
    try:
        doc = fitz.open(str(pdf_path))
        for pno, page in enumerate(doc, start=1):
            for bi, b in enumerate(page.get_text("blocks")):
                if len(b) < 7 or b[6] != 0:
                    continue
                x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
                text = (text or "").strip()
                if text:
                    blocks.append({
                        "id": f"U{pno:04d}{bi:03d}", "page": pno,
                        "x0": round(float(x0), 1), "y0": round(float(y0), 1),
                        "x1": round(float(x1), 1), "y1": round(float(y1), 1),
                        "text": text, "type": "text", "col": 0,
                    })
        doc.close()
    except Exception:
        pass
    return blocks


def _blocks_from_pdfplumber(pdf_path: Path, plumber) -> list:
    blocks = []
    try:
        with plumber.open(str(pdf_path)) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                lines = {}
                for w in words:
                    key = round(w.get("top", 0) / 6.0)
                    lines.setdefault(key, []).append(w)
                for li, (key, ws) in enumerate(sorted(lines.items())):
                    ws = sorted(ws, key=lambda w: w.get("x0", 0))
                    text = " ".join((w.get("text", "") or "") for w in ws)
                    if text.strip():
                        blocks.append({
                            "id": f"U{pno:04d}{li:03d}", "page": pno,
                            "x0": round(min((w.get("x0", 0) for w in ws)), 1),
                            "y0": round(min((w.get("top", 0) for w in ws)), 1),
                            "x1": round(max((w.get("x1", 0) for w in ws)), 1),
                            "y1": round(max((w.get("bottom", 0) for w in ws)), 1),
                            "text": text.strip(), "type": "text", "col": 0,
                        })
    except Exception:
        pass
    return blocks


def _blocks_from_pdftotext_bbox(pdf_path: Path) -> list:
    blocks = []
    try:
        rc, out, _ = C.run_cmd(["pdftotext", "-bbox", str(pdf_path), "-"], timeout=300)
        if rc != 0:
            return blocks
        pages = re.split(r"<page width=\"[\d.]+\" height=\"[\d.]+\">", out)
        for pno, chunk in enumerate(pages[1:], start=1):
            ws = []
            for m in re.finditer(
                r"<word xMin=\"([\d.]+)\" yMin=\"([\d.]+)\" xMax=\"([\d.]+)\" yMax=\"([\d.]+)\">([^<]*)</word>",
                chunk,
            ):
                ws.append((float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4)), m.group(5)))
            lines = {}
            for (x0, y0, x1, y1, t) in ws:
                lines.setdefault(round(y0 / 6.0), []).append((x0, y0, x1, y1, t))
            for li, (key, items) in enumerate(sorted(lines.items())):
                items = sorted(items, key=lambda it: it[0])
                text = " ".join(it[4] for it in items)
                if text.strip():
                    blocks.append({
                        "id": f"U{pno:04d}{li:03d}", "page": pno,
                        "x0": min(it[0] for it in items), "y0": min(it[1] for it in items),
                        "x1": max(it[2] for it in items), "y1": max(it[3] for it in items),
                        "text": text.strip(), "type": "text", "col": 0,
                    })
    except Exception:
        pass
    return blocks


def _blocks_from_pdftotext_layout(pdf_path: Path) -> list:
    blocks = []
    try:
        rc, out, _ = C.run_cmd(["pdftotext", "-layout", str(pdf_path), "-"], timeout=300)
        if rc != 0:
            return blocks
        pages = out.split("\f")
        for pno, page in enumerate(pages, start=1):
            y = 0
            for line in page.splitlines():
                if line.strip():
                    blocks.append({
                        "id": f"U{pno:04d}{y:03d}", "page": pno,
                        "x0": 0, "y0": y, "x1": 0, "y1": y + 12,
                        "text": line.strip(), "type": "text", "col": 0,
                    })
                y += 12
    except Exception:
        pass
    return blocks


def extract_blocks(pdf_path: Path) -> list:
    fitz = _try_import_fitz()
    if fitz:
        blocks = _blocks_from_fitz(pdf_path, fitz)
        if blocks:
            return blocks
    plumber = _try_import_pdfplumber()
    if plumber:
        blocks = _blocks_from_pdfplumber(pdf_path, plumber)
        if blocks:
            return blocks
    blocks = _blocks_from_pdftotext_bbox(pdf_path)
    if blocks:
        return blocks
    return _blocks_from_pdftotext_layout(pdf_path)


# ---------------------------------------------------------------------------
# Column / reading order
# ---------------------------------------------------------------------------

def _detect_column_split(blocks: list, page: int) -> float | None:
    page_blocks = [b for b in blocks if b["page"] == page
                   and b["type"] in ("text", "heading", "formula")
                   and b["x1"] > b["x0"]]
    if len(page_blocks) < 6:
        return None
    xs = [b for b in page_blocks if b["x1"] > b["x0"]]
    if not xs:
        return None
    w_min = min(b["x0"] for b in xs)
    w_max = max(b["x1"] for b in xs)
    width = w_max - w_min
    if width < 380:
        return None
    narrow = [(b["x0"] + b["x1"]) / 2 for b in xs if (b["x1"] - b["x0"]) < 0.55 * width]
    if len(narrow) < 6:
        return None
    narrow.sort()
    best_gap, best_pos = 0.0, None
    for i in range(len(narrow) - 1):
        gap = narrow[i + 1] - narrow[i]
        if gap > best_gap:
            best_gap = gap
            best_pos = (narrow[i] + narrow[i + 1]) / 2
    if best_gap >= 24 and best_pos is not None:
        left = sum(1 for c in narrow if c < best_pos)
        right = len(narrow) - left
        if left >= 3 and right >= 3:
            return best_pos
    return None


def assign_columns(blocks: list) -> list:
    pages = sorted({b["page"] for b in blocks})
    ordered = []
    for page in pages:
        page_blocks = [b for b in blocks if b["page"] == page]
        split = _detect_column_split(page_blocks, page)
        xs = [b for b in page_blocks if b["x1"] > b["x0"]]
        width = (max(b["x1"] for b in xs) - min(b["x0"] for b in xs)) if xs else 0
        for b in page_blocks:
            span = b["x1"] - b["x0"]
            if width > 0 and span > 0.55 * width:
                b["col"] = 0
            elif split is not None:
                c = (b["x0"] + b["x1"]) / 2
                b["col"] = 0 if c <= split else 1
            else:
                b["col"] = 0
        page_blocks.sort(key=lambda b: (b["col"], b["y0"], b["x0"]))
        ordered.extend(page_blocks)
    return ordered


# ---------------------------------------------------------------------------
# Semantic classification
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(
    r"^\d+(\.\d+)*[\.\)]?\s+\S|"
    r"^(?:Abstract|Introduction|Background|Methods?|Materials?|Results?|"
    r"Discussion|Conclusion|Conclusions|References?|Acknowledgments?|"
    r"Appendix|Supplementary|Related Works?|"
    r"摘要|引言|方法|结果|讨论|结论|参考文献|致谢|附录)\s*$",
    re.IGNORECASE,
)
_CAPTION_RE = re.compile(
    r"^(Fig(?:ure)?\.?\s*\d+|图\s*\d+|Table\s+\d+|表\s*\d+|Scheme\s+\d+|Algorithm\s+\d+)",
    re.IGNORECASE,
)
_REF_MARKER_RE = re.compile(r"^\[\d+\]|^\[\d+[-,]\d+\]")
_FORMULA_HINT_RE = re.compile(r"[=<>≤≥±×÷∫∑∏√∞∂∇≈≠]|\\[a-zA-Z]+")


def classify_blocks(blocks: list) -> list:
    in_refs = False
    for b in blocks:
        text = b["text"].strip()
        if _CAPTION_RE.match(text):
            b["type"] = "caption"
        elif _HEADING_RE.match(text) and len(text) <= 80:
            b["type"] = "heading"
        elif re.match(r"^(References?|参考文献)\b", text, re.IGNORECASE):
            b["type"] = "heading"
            in_refs = True
        elif _REF_MARKER_RE.match(text) or in_refs:
            b["type"] = "ref"
        elif text.count("=") >= 1 and len(text) < 160 and _FORMULA_HINT_RE.search(text):
            b["type"] = "formula"
        else:
            b["type"] = "text"
    return blocks


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def build_segments(blocks: list) -> list:
    """Turn ordered blocks into stable translation units S+page+seq.

    Consecutive short text fragments in the same column are merged so a
    paragraph split by a soft line break stays one unit; captions/formulas/
    headings/refs are kept as their own units so their numbers can be checked.
    """
    segments = []
    buf = None  # open paragraph {page, col, type, parts}

    def flush():
        nonlocal buf
        if buf is not None:
            text = " ".join(buf["parts"]).strip()
            if text:
                segments.append({"id": "", "page": buf["page"], "col": buf["col"],
                                 "type": buf["type"], "text": text})
            buf = None

    for b in blocks:
        t = b["type"]
        if t == "text":
            if buf is not None and buf["page"] == b["page"] and buf["col"] == b["col"] \
                    and len(b["text"]) < 80 and not re.search(r"[.!?。！？]\s*$", buf["parts"][-1]):
                buf["parts"].append(b["text"])
            else:
                flush()
                buf = {"page": b["page"], "col": b["col"], "type": "text", "parts": [b["text"]]}
        else:
            flush()
            segments.append({"id": "", "page": b["page"], "col": b["col"],
                             "type": t, "text": b["text"].strip()})
    flush()

    # stable ids: S + 4-digit page + 3-digit sequence
    for i, s in enumerate(segments, start=1):
        s["id"] = f"S{s['page']:04d}-{i:03d}"
        s["src_chars"] = len(s["text"])
    return segments


# ---------------------------------------------------------------------------
# Glossary candidates
# ---------------------------------------------------------------------------

_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}\b")
_TITLE_PHRASE_RE = re.compile(r"\b(?:[A-Z][a-z]{2,}(?:-[A-Z][a-z]{2,})*[ \t]+){1,3}[A-Z][a-z]{2,}(?:-[A-Z][a-z]{2,})*\b")


def extract_glossary_candidates(segments: list, corpus: str, limit: int = 60) -> list:
    """Deterministic candidate extraction (never locks a translation).

    Candidates = acronyms + title-case phrases. Each candidate carries a
    verbatim quote from the corpus so downstream can enforce "no quote, no lock".
    """
    norm_corpus = C.normalize_quote(corpus)
    candidates = {}
    # acronyms first (cheap, high precision)
    for m in _ACRONYM_RE.finditer(corpus):
        term = m.group(0)
        if term in ("I", "A", "The", "An", "Fig", "Table", "URL", "DOI", "HTTP", "PDF"):
            continue
        candidates.setdefault(term, 0)
        candidates[term] += 1
    # title-case phrases
    for m in _TITLE_PHRASE_RE.finditer(corpus):
        term = m.group(0).strip()
        if 6 <= len(term) <= 60:
            candidates.setdefault(term, 0)
            candidates[term] += 1
    # sort by frequency then length (more specific first)
    ranked = sorted(candidates.items(), key=lambda kv: (-kv[1], -len(kv[0])))[:limit]
    out = []
    for term, freq in ranked:
        q = C.normalize_quote(term)
        quote = term if q in norm_corpus else ""
        out.append({
            "source": term, "target": "", "status": "candidate", "freq": freq,
            "evidence": {"quote": quote} if quote else {},
        })
    return out


# ---------------------------------------------------------------------------
# Plain-text dump (for quote verification)
# ---------------------------------------------------------------------------

def write_extracted_text(segments: list, out_path: Path) -> str:
    pages = sorted({s["page"] for s in segments})
    lines = []
    for page in pages:
        lines.append(f"\n\n===== PAGE {page} =====\n")
        for s in segments:
            if s["page"] == page:
                lines.append(f"[{s['id']}] {s['text']}")
    text = "\n".join(lines)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    except Exception:
        pass
    return text


# ---------------------------------------------------------------------------
# Input discovery (PDF or embedded text)
# ---------------------------------------------------------------------------

def find_material(in_dir: Path):
    """Return (kind, path, text). kind in pdf|text|embedded|none."""
    disc = C.discover_inputs(in_dir)
    if disc["pdfs"]:
        return "pdf", in_dir / disc["pdfs"][0], ""
    # look for plain-text / markdown / docx / html material
    for rel in disc["files"]:
        p = in_dir / rel
        low = p.name.lower()
        if low.endswith((".txt", ".md", ".markdown")):
            return "text", p, C.read_text(p)
        if low.endswith(".html") or low.endswith(".htm"):
            html = C.read_text(p)
            text = re.sub(r"<[^>]+>", " ", html)
            return "text", p, text
    # case.json embedded body
    for rel in disc["case_json"]:
        case = C.read_json(in_dir / rel, {})
        body = _find_embedded_text(case)
        if body:
            return "embedded", in_dir / rel, body
    return "none", None, ""


def _find_embedded_text(node, depth=0):
    if depth > 6:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("text", "body", "content", "fulltext", "full_text", "abstract"):
            v = node.get(key)
            if isinstance(v, str) and len(v) > 80:
                return v
        for v in node.values():
            r = _find_embedded_text(v, depth + 1)
            if r:
                return r
    elif isinstance(node, list):
        for item in node:
            r = _find_embedded_text(item, depth + 1)
            if r:
                return r
    return ""


def _segments_from_text(text: str, page: int = 1) -> list:
    """Fallback for TXT/DOCX/HTML/embedded body: split by blank lines."""
    paras = re.split(r"\n\s*\n", text)
    segments = []
    for i, p in enumerate(paras, start=1):
        p = p.strip()
        if not p:
            continue
        segments.append({"id": f"S{page:04d}-{i:03d}", "page": page, "col": 0,
                         "type": "text", "text": p[:2000], "src_chars": len(p[:2000])})
    return segments


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def prepare(in_dir: Path, wk_dir: Path, out_dir: Path, result: dict) -> dict:
    started = time.time()
    kind, path, text = find_material(in_dir)
    meta = {"kind": kind, "file": str(path) if path else "", "pages": 0, "text_layer": None}
    segments = []
    if kind == "pdf":
        meta["pages"] = page_count(path)
        meta["text_layer"] = text_layer_present(path)
        blocks = extract_blocks(path)
        blocks = assign_columns(blocks)
        blocks = classify_blocks(blocks)
        segments = build_segments(blocks)
    elif kind in ("text", "embedded"):
        segments = _segments_from_text(text)
        meta["text_layer"] = True
    else:
        C.record_error(result, "no readable material found (no PDF/TXT/DOCX/HTML or embedded body)", "prepare.material")
        result["status"] = "failed"
        result["summary"] = "No readable input material discovered."
        C.write_result(result)
        return {"segments": [], "glossary": [], "meta": meta,
                "segment_count": 0, "glossary_candidate_count": 0}

    corpus = "\n".join(s["text"] for s in segments)
    glossary = extract_glossary_candidates(segments, corpus)
    prepared = {
        "meta": meta,
        "segments": segments,
        "glossary": glossary,
        "segment_count": len(segments),
        "glossary_candidate_count": len(glossary),
        "prepared_at": C._now(),
    }
    wk_dir.mkdir(parents=True, exist_ok=True)
    C.write_json(wk_dir / "segments.json", {"meta": meta, "segments": segments})
    C.write_json(wk_dir / "glossary.json", {"glossary": glossary})
    write_extracted_text(segments, out_dir / "extracted_text.md")

    result.setdefault("sources", []).append({
        "id": f"S{len(result['sources']) + 1}",
        "file": meta["file"], "kind": meta["kind"], "pages": meta["pages"],
        "readable": bool(segments), "text_layer": meta["text_layer"],
    })
    result["runtime"]["elapsed_seconds"] = round(time.time() - started, 1)
    if not segments:
        C.record_error(result, "material discovered but no segments built", "prepare.segment")
    if kind == "pdf" and meta["text_layer"] is False:
        C.record_degradation(result, f"{Path(meta['file']).name} has no text layer; OCR required", "prepare")
    return prepared


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["inventory", "prepare", "run"])
    args = ap.parse_args(argv)

    C.ensure_dirs()
    in_dir, wk_dir, out_dir = C.input_dir(), C.work_dir(), C.output_dir()
    result = C.load_result()
    t0 = time.time()

    if args.command == "inventory":
        kind, path, _ = find_material(in_dir)
        print(json.dumps({"kind": kind, "file": str(path)}, ensure_ascii=False, indent=2))
        return 0

    result["status"] = "partial"
    result["summary"] = "Preparing material: discovering input and segmenting for translation."
    C.write_result(result)
    prepared = prepare(in_dir, wk_dir, out_dir, result)
    result["summary"] = (
        f"Prepared {prepared['segment_count']} segments, "
        f"{prepared['glossary_candidate_count']} glossary candidates from {prepared['meta']['kind']} material."
    )
    result["runtime"]["elapsed_seconds"] = round(time.time() - t0, 1)
    C.write_result(result)
    print(json.dumps({
        "status": result["status"], "segments": prepared["segment_count"],
        "glossary_candidates": prepared["glossary_candidate_count"],
        "kind": prepared["meta"]["kind"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            C.ensure_dirs()
            r = C.load_result()
            C.record_error(r, f"{type(exc).__name__}: {exc}", "prepare.crash")
            if r.get("status") in (None, "partial"):
                r["status"] = "failed"
            C.write_result(r)
        except Exception:
            pass
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(0)
