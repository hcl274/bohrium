#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inventory and parse an academic PDF into page-level structured blocks.

Output (work/parsed.json): source metadata, per-page blocks with bounding
boxes and a corrected multi-column reading order, detected sections, tables,
figures, formulas and references. Also writes a page-marked plain-text dump
(output/extracted_text.md) that downstream evidence quotes are matched
against.

Extraction ladder (each step falls back to the next):
  1. PyMuPDF (fitz)  -- blocks + spans + native table finder + images
  2. pdfplumber     -- words/lines + extract_tables
  3. pdftotext -bbox (XML)  -- word-level coordinates
  4. pdftotext -layout      -- plain text, no coordinates (last resort)

Usage:
  python3 parse_pdf.py inventory      # list inputs + read/parse status
  python3 parse_pdf.py parse          # full parse -> work/parsed.json
  python3 parse_pdf.py run            # inventory + parse + write partial result
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
# Optional third-party imports (guarded, import-time safe)
# ---------------------------------------------------------------------------

def _try_import_fitz():
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            import pymupdf  # PyMuPDF (new name)
            return pymupdf
        except Exception:
            pass
        try:
            import fitz  # PyMuPDF (legacy name)
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
# Inventory
# ---------------------------------------------------------------------------

def page_count(pdf_path: Path) -> int:
    rc, out, _ = C.run_cmd(["pdfinfo", str(pdf_path)], timeout=60)
    if rc == 0:
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        if m:
            return int(m.group(1))
    return 0


def text_layer_present(pdf_path: Path, pages_to_probe: int = 3) -> bool:
    """Heuristic: run pdftotext on the first few pages and check non-whitespace."""
    rc, out, _ = C.run_cmd(
        ["pdftotext", "-f", "1", "-l", str(pages_to_probe), str(pdf_path), "-"],
        timeout=120,
    )
    if rc != 0:
        return False
    return len(re.sub(r"\s+", "", out)) >= 40


def inventory(input_root: Path) -> dict:
    disc = C.discover_inputs(input_root)
    inv = {"input_dir": str(input_root), "pdfs": [], "case_json": disc["case_json"],
           "task_md": disc["task_md"]}
    for rel in disc["pdfs"]:
        p = input_root / rel
        inv["pdfs"].append({
            "file": rel,
            "abs": str(p),
            "pages": page_count(p),
            "text_layer": text_layer_present(p),
        })
    return inv


# ---------------------------------------------------------------------------
# Low-level extraction: return per-page list of blocks
# ---------------------------------------------------------------------------
# Block shape:
#   {"id", "page", "x0", "y0", "x1", "y1", "text", "type", "col"}
# type is one of text | heading | table | figure | formula | caption | ref

def _make_blocks_from_fitz(pdf_path: Path, fitz) -> list:
    blocks = []
    try:
        doc = fitz.open(str(pdf_path))
        for pno, page in enumerate(doc, start=1):
            # get_text("blocks") preserves inter-word spacing (the "dict" path
            # returns word-sized spans whose concatenation drops the spaces).
            for bi, b in enumerate(page.get_text("blocks")):
                # b = (x0, y0, x1, y1, text, block_no, block_type)
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
            # images -> figure blocks
            try:
                for img in page.get_images(full=True):
                    try:
                        rects = page.get_image_rects(img[0])
                        for r in rects:
                            blocks.append({
                                "id": f"IMG{pno:04d}{len(blocks):03d}", "page": pno,
                                "x0": round(r.x0, 1), "y0": round(r.y0, 1),
                                "x1": round(r.x1, 1), "y1": round(r.y1, 1),
                                "text": "", "type": "figure", "col": 0,
                            })
                    except Exception:
                        pass
            except Exception:
                pass
        doc.close()
    except Exception:
        pass
    return blocks


def _make_blocks_from_pdfplumber(pdf_path: Path, plumber) -> list:
    blocks = []
    try:
        with plumber.open(str(pdf_path)) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                # group words into lines by y
                lines = {}
                for w in words:
                    key = round(w.get("top", 0) / 6.0)
                    lines.setdefault(key, []).append(w)
                for li, (key, ws) in enumerate(sorted(lines.items())):
                    ws = sorted(ws, key=lambda w: w.get("x0", 0))
                    text = " ".join((w.get("text", "") or "") for w in ws)
                    x0 = min((w.get("x0", 0) for w in ws))
                    y0 = min((w.get("top", 0) for w in ws))
                    x1 = max((w.get("x1", 0) for w in ws))
                    y1 = max((w.get("bottom", 0) for w in ws))
                    if text.strip():
                        blocks.append({
                            "id": f"U{pno:04d}{li:03d}", "page": pno,
                            "x0": round(x0, 1), "y0": round(y0, 1),
                            "x1": round(x1, 1), "y1": round(y1, 1),
                            "text": text.strip(), "type": "text", "col": 0,
                        })
    except Exception:
        pass
    return blocks


def _make_blocks_from_pdftotext_bbox(pdf_path: Path) -> list:
    blocks = []
    try:
        rc, out, _ = C.run_cmd(["pdftotext", "-bbox", str(pdf_path), "-"], timeout=300)
        if rc != 0:
            return blocks
        page_words = {}
        for m in re.finditer(r"<word xMin=\"([\d.]+)\" yMin=\"([\d.]+)\" xMax=\"([\d.]+)\" yMax=\"([\d.]+)\">([^<]*)</word>", out):
            page = page_words.setdefault(1, [])
            page.append((float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4)), m.group(5)))
        # pdftotext -bbox does not emit page breaks in a reliable way for
        # grouping; fall back to a single-page treatment is lossy. Instead,
        # split by <page> elements when present.
        pages = re.split(r"<page width=\"[\d.]+\" height=\"[\d.]+\">", out)
        for pno, chunk in enumerate(pages[1:], start=1):
            ws = []
            for m in re.finditer(r"<word xMin=\"([\d.]+)\" yMin=\"([\d.]+)\" xMax=\"([\d.]+)\" yMax=\"([\d.]+)\">([^<]*)</word>", chunk):
                ws.append((float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4)), m.group(5)))
            lines = {}
            for (x0, y0, x1, y1, t) in ws:
                key = round(y0 / 6.0)
                lines.setdefault(key, []).append((x0, y0, x1, y1, t))
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


def _make_blocks_from_pdftotext_layout(pdf_path: Path) -> list:
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
        blocks = _make_blocks_from_fitz(pdf_path, fitz)
        if blocks:
            return blocks
    plumber = _try_import_pdfplumber()
    if plumber:
        blocks = _make_blocks_from_pdfplumber(pdf_path, plumber)
        if blocks:
            return blocks
    blocks = _make_blocks_from_pdftotext_bbox(pdf_path)
    if blocks:
        return blocks
    return _make_blocks_from_pdftotext_layout(pdf_path)


# ---------------------------------------------------------------------------
# Column detection and reading-order correction
# ---------------------------------------------------------------------------

def _detect_column_split(blocks: list, page: int) -> float | None:
    """Return the gutter x-center for a two-column page, or None if single-column.

    Uses the largest gap between consecutive x-centers of *narrow* body blocks.
    Full-width blocks (title/authors/wide tables) are ignored because they span
    the gutter and would mask it.
    """
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
    if width < 380:  # page too narrow for two meaningful columns (pt)
        return None
    # Keep only blocks whose own width is less than ~55% of the page width.
    narrow = [(b["x0"] + b["x1"]) / 2 for b in xs if (b["x1"] - b["x0"]) < 0.55 * width]
    if len(narrow) < 6:
        return None
    narrow.sort()
    # Find the largest gap between consecutive centers in the middle band.
    best_gap, best_pos = 0.0, None
    for i in range(len(narrow) - 1):
        gap = narrow[i + 1] - narrow[i]
        if gap > best_gap:
            best_gap = gap
            best_pos = (narrow[i] + narrow[i + 1]) / 2
    # A gutter of >= ~24pt with blocks on both sides suggests two columns.
    if best_gap >= 24 and best_pos is not None:
        left = sum(1 for c in narrow if c < best_pos)
        right = len(narrow) - left
        if left >= 3 and right >= 3:
            return best_pos
    return None


def assign_columns(blocks: list) -> list:
    """Assign col index and sort in reading order (column-major)."""
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
                # Full-width block (title/authors/wide table): sort with the
                # left column by its y position, not pushed to the right column.
                b["col"] = 0
            elif split is not None:
                c = (b["x0"] + b["x1"]) / 2
                b["col"] = 0 if c <= split else 1
            else:
                b["col"] = 0
        # Reading order: column-major, then top-to-bottom; figures/captions
        # interleave by their top position within the same column.
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
    r"^(Fig(?:ure)?\.?\s*\d+|图\s*\d+|Table\s+\d+|表\s*\d+|"
    r"Scheme\s+\d+|Algorithm\s+\d+|Equation\s*\d+)",
    re.IGNORECASE,
)

_REF_MARKER_RE = re.compile(r"^\[\d+\]|^\[\d+[-,]\d+\]")

_FORMULA_HINT_RE = re.compile(r"[=<>≤≥±×÷∫∑∏√∞∂∇≈≠]|\\[a-zA-Z]+")


def classify_blocks(blocks: list) -> list:
    in_refs = False
    for b in blocks:
        text = b["text"].strip()
        t = "text"
        if b.get("type") == "figure":
            t = "figure"
        elif _CAPTION_RE.match(text):
            t = "caption"
        elif _HEADING_RE.match(text) and len(text) <= 80:
            t = "heading"
        elif _REF_MARKER_RE.match(text) or in_refs:
            t = "ref"
        if re.match(r"^(References?|参考文献)\b", text, re.IGNORECASE):
            in_refs = True
        if t == "text" and _FORMULA_HINT_RE.search(text) and len(text) < 200:
            # only mark as formula if it looks equation-heavy
            if text.count("=") >= 1 and len(text) < 160:
                t = "formula"
        b["type"] = t
    return blocks


# ---------------------------------------------------------------------------
# Sections / tables / figures / formulas / references aggregation
# ---------------------------------------------------------------------------

def _build_sections(blocks: list) -> list:
    sections = []
    for b in blocks:
        if b["type"] == "heading":
            level = 1
            if re.match(r"^\d+\.\d+", b["text"]):
                level = 2
            sections.append({"heading": b["text"], "page": b["page"], "level": level,
                             "block": b["id"]})
    if not sections:
        sections.append({"heading": "Full text", "page": 1, "level": 1, "block": ""})
    return sections


def _extract_tables(pdf_path: Path, blocks: list) -> list:
    tables = []
    fitz = _try_import_fitz()
    if fitz:
        try:
            doc = fitz.open(str(pdf_path))
            for pno, page in enumerate(doc, start=1):
                try:
                    found = page.find_tables()
                    for ti, tab in enumerate(found.tables, start=1):
                        rows = tab.extract() or []
                        if rows:
                            tables.append({
                                "id": f"T{pno:02d}-{ti}", "page": pno,
                                "bbox": [round(v, 1) for v in tab.bbox],
                                "rows": [[c if c is not None else "" for c in r] for r in rows],
                                "method": "fitz.find_tables",
                            })
                except Exception:
                    pass
            doc.close()
        except Exception:
            pass
    if tables:
        return tables
    plumber = _try_import_pdfplumber()
    if plumber:
        try:
            with plumber.open(str(pdf_path)) as pdf:
                for pno, page in enumerate(pdf.pages, start=1):
                    for ti, tab in enumerate(page.extract_tables(), start=1):
                        if tab:
                            tables.append({
                                "id": f"T{pno:02d}-{ti}", "page": pno,
                                "bbox": None, "rows": tab,
                                "method": "pdfplumber.extract_tables",
                            })
        except Exception:
            pass
    return tables


def _extract_figures(blocks: list) -> list:
    figures = []
    for b in blocks:
        if b["type"] == "figure":
            figures.append({"id": f"F{b['page']:02d}-{len(figures)+1}", "page": b["page"],
                            "bbox": [b["x0"], b["y0"], b["x1"], b["y1"]], "block": b["id"]})
    # attach captions to the nearest figure on the same page below it
    captions = [b for b in blocks if b["type"] == "caption"]
    for cap in captions:
        cand = [f for f in figures if f["page"] == cap["page"] and f["bbox"][3] <= cap["y0"]]
        if cand:
            target = max(cand, key=lambda f: f["bbox"][3])
            target["caption"] = cap["text"]
            target["caption_block"] = cap["id"]
    return figures


def _extract_formulas(blocks: list) -> list:
    return [{"id": f"E{b['page']:02d}-{i+1}", "page": b["page"], "text": b["text"],
             "block": b["id"]}
            for i, b in enumerate(b for b in blocks if b["type"] == "formula")]


def _extract_references(blocks: list) -> list:
    refs = [b for b in blocks if b["type"] == "ref"]
    return [{"id": f"R{b['page']:02d}-{i+1}", "page": b["page"], "text": b["text"],
             "block": b["id"]} for i, b in enumerate(refs)]


def _extract_title_meta(blocks: list) -> dict:
    # First page, top blocks, before the first heading.
    page1 = sorted([b for b in blocks if b["page"] == 1], key=lambda b: (b["y0"], b["x0"]))
    title = ""
    authors = ""
    abstract = ""
    abstract_col = None
    for b in page1[:40]:
        t = b["text"].strip()
        if not title and b["type"] in ("text", "heading") and len(t) > 8 and not _CAPTION_RE.match(t):
            title = t
        if re.match(r"^Abstract\b", t, re.IGNORECASE):
            abstract_col = b.get("col", 0)
            continue
        if abstract_col is not None and b.get("col", 0) == abstract_col:
            # Stop at the next heading (e.g. "1 Introduction").
            if b["type"] == "heading":
                break
            if b["type"] == "text":
                abstract += t + " "
                if len(abstract) > 1800:
                    break
    return {"title": title[:300], "authors": authors, "abstract": abstract.strip()[:2000]}


# ---------------------------------------------------------------------------
# Text dump (page-marked, for quote verification)
# ---------------------------------------------------------------------------

def write_extracted_text(blocks: list, out_path: Path) -> str:
    pages = sorted({b["page"] for b in blocks})
    lines = []
    for page in pages:
        lines.append(f"\n\n===== PAGE {page} =====\n")
        for b in blocks:
            if b["page"] == page and b["type"] in ("text", "heading", "caption", "formula", "ref"):
                lines.append(b["text"])
    text = "\n".join(lines)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    except Exception:
        pass
    return text


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def parse(pdf_path: Path, out_dir: Path, work_dir: Path, result: dict) -> dict:
    started = time.time()
    meta = {
        "file": str(pdf_path),
        "pages": page_count(pdf_path),
        "text_layer": text_layer_present(pdf_path),
    }
    blocks = extract_blocks(pdf_path)
    blocks = assign_columns(blocks)
    blocks = classify_blocks(blocks)
    parsed = {
        "source": meta,
        "title_meta": _extract_title_meta(blocks),
        "pages": sorted({b["page"] for b in blocks}),
        "blocks": blocks,
        "sections": _build_sections(blocks),
        "tables": _extract_tables(pdf_path, blocks),
        "figures": _extract_figures(blocks),
        "formulas": _extract_formulas(blocks),
        "references": _extract_references(blocks),
        "parsed_at": C._now(),
    }
    work_dir.mkdir(parents=True, exist_ok=True)
    C.write_json(work_dir / "parsed.json", parsed)
    text = write_extracted_text(blocks, out_dir / "extracted_text.md")
    result.setdefault("sources", []).append({
        "id": f"S{len(result['sources'])+1}",
        "file": str(pdf_path), "kind": "pdf", "pages": meta["pages"],
        "readable": bool(blocks), "text_layer": meta["text_layer"],
    })
    result["runtime"]["elapsed_seconds"] = round(time.time() - started, 1)
    if not blocks:
        C.record_error(result, f"no text extracted from {pdf_path.name} (may be scanned or encrypted)", "parse")
    if not meta["text_layer"]:
        C.record_degradation(result, f"{pdf_path.name} has no usable text layer; OCR required", "parse")
    return parsed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["inventory", "parse", "run"])
    ap.add_argument("--pdf", help="explicit PDF path (otherwise auto-discover)")
    args = ap.parse_args(argv)

    C.ensure_dirs()
    in_dir, wk_dir, out_dir = C.input_dir(), C.work_dir(), C.output_dir()
    result = C.load_result()
    t0 = time.time()

    if args.command == "inventory":
        inv = inventory(in_dir)
        print(json.dumps(inv, ensure_ascii=False, indent=2))
        return 0

    # discover pdf
    pdf_path = None
    if args.pdf and Path(args.pdf).exists():
        pdf_path = Path(args.pdf)
    else:
        disc = C.discover_inputs(in_dir)
        if disc["pdfs"]:
            pdf_path = in_dir / disc["pdfs"][0]
    if pdf_path is None:
        C.record_error(result, "no PDF found in input directory", "parse.discover")
        result["status"] = "failed"
        result["summary"] = "No PDF input discovered."
        C.write_result(result)
        print(json.dumps({"status": "failed", "reason": "no pdf"}, ensure_ascii=False))
        return 0

    if args.command in ("parse", "run"):
        # Write an honest partial result.json *before* parsing so a kill/timeout
        # still leaves a well-formed artifact for the harness to inspect.
        result["status"] = "partial"
        result["summary"] = f"Parsing started for {pdf_path.name}."
        C.write_result(result)
        parsed = parse(pdf_path, out_dir, wk_dir, result)
        result["summary"] = (
            f"Parsed {parsed['source']['file']} ({parsed['source']['pages']} pages, "
            f"{len(parsed['blocks'])} blocks, {len(parsed['sections'])} sections, "
            f"{len(parsed['tables'])} tables, {len(parsed['figures'])} figures, "
            f"{len(parsed['formulas'])} formulas, {len(parsed['references'])} references)."
        )
        result["runtime"]["elapsed_seconds"] = round(time.time() - t0, 1)
        C.write_result(result)
        print(json.dumps({"status": result["status"], "blocks": len(parsed["blocks"]),
                          "sections": len(parsed["sections"]), "tables": len(parsed["tables"]),
                          "figures": len(parsed["figures"]), "formulas": len(parsed["formulas"]),
                          "references": len(parsed["references"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # Guarantee a result.json exists even on catastrophic failure.
        try:
            C.ensure_dirs()
            r = C.load_result()
            C.record_error(r, f"{type(exc).__name__}: {exc}", "parse.crash")
            r["status"] = "failed" if r.get("status") in (None, "partial") else r["status"]
            C.write_result(r)
        except Exception:
            pass
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(0)
