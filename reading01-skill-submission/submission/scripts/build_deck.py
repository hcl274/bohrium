#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build an editable academic slide deck from the parsed paper structure.

Reads work/parsed.json and writes, under the output directory:
  deck.pptx            editable PowerPoint
  outline.md           slide-by-slide outline with page-level evidence
  speaker_notes.md     full speaker notes (讲稿/备注)
  evidence_map.json    slide -> source page(s)/block ids mapping
  figures/             extracted figure images (when available)

Every slide carries speaker notes that restate the *verbatim* source text plus
its page/block locator, so the deck is inspectable and re-checkable.

PPTX backend ladder: python-pptx -> minimal OOXML via stdlib (zipfile).

Usage:
  python3 build_deck.py            # build everything from work/parsed.json
  python3 build_deck.py --dry-run  # validate inputs, no output writes
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import _common as C

try:
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    HAS_PPTX = True
except Exception:
    HAS_PPTX = False

try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            import pymupdf as fitz
        except Exception:
            import fitz
    HAS_FITZ = True
except Exception:
    HAS_FITZ = False

try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_parsed(work_dir: Path) -> dict | None:
    return C.read_json(work_dir / "parsed.json")


def _section_key_points(blocks: list, section: dict, next_section: dict | None) -> list:
    """Return blocks between this section heading and the next heading."""
    out = []
    started = False
    for b in blocks:
        if b["id"] == section.get("block"):
            started = True
            continue
        if started:
            if next_section and b["id"] == next_section.get("block"):
                break
            if b["type"] in ("text", "caption"):
                t = b["text"].strip()
                if t and len(t) > 12:
                    out.append({"text": t[:600], "page": b["page"], "block": b["id"]})
            if len(out) >= 6:
                break
    return out


def _render_figure_png(pdf_path: Path, page: int, bbox, out_path: Path, scale: float = 2.0) -> bool:
    """Render a figure region to PNG. bbox is [x0, y0, x1, y1] in points."""
    try:
        if HAS_FITZ:
            doc = fitz.open(str(pdf_path))
            pg = doc.load_page(page - 1)
            clip = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
            pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            pix.save(str(out_path))
            doc.close()
            return True
        else:
            # Fall back: render full page with pdftoppm, then crop with PIL.
            tmp_png = out_path.with_suffix(".full.png")
            rc, _, err = C.run_cmd(
                ["pdftoppm", "-png", "-r", str(int(72 * scale)), "-f", str(page),
                 "-l", str(page), str(pdf_path), str(tmp_png.with_suffix(""))],
                timeout=180,
            )
            if rc != 0 or not HAS_PIL:
                return False
            # pdftoppm names output as prefix-01.png etc.; find it.
            candidates = sorted(Path(tmp_png.parent).glob(f"{tmp_png.stem}*.png"))
            if not candidates:
                return False
            img = Image.open(candidates[0])
            # bbox is in points; at `scale` * 72 dpi -> 1 pt = scale px
            box = (int(bbox[0] * scale), int(bbox[1] * scale),
                   int(bbox[2] * scale), int(bbox[3] * scale))
            cropped = img.crop(box)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            cropped.save(str(out_path))
            for c in candidates:
                try:
                    c.unlink()
                except Exception:
                    pass
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# python-pptx backend
# ---------------------------------------------------------------------------

def _add_text_slide(prs, title: str, body: str, notes: str) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[1])  # Title and Content
    slide.shapes.title.text = title
    slide.placeholders[1].text = body
    try:
        slide.notes_slide.notes_text_frame.text = notes
    except Exception:
        pass


def _build_pptx_rich(parsed: dict, out_path: Path, figure_dir: Path) -> None:
    prs = Presentation()
    blocks = parsed.get("blocks", [])
    meta = parsed.get("title_meta", {})
    source = parsed.get("source", {})
    sections = parsed.get("sections", [])
    pdf_path = source.get("file", "")

    def notes_for(page_refs, text) -> str:
        loc = ", ".join(f"p.{p}" for p in sorted({x[0] for x in page_refs}))
        return f"【证据定位】{loc}\n【原文】{text}"

    # 1. Title slide
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = meta.get("title") or "Untitled paper"
    subtitle = meta.get("authors") or ""
    if slide.placeholders[1]:
        slide.placeholders[1].text = subtitle or f"Source: {Path(pdf_path).name} · {source.get('pages', '?')} pages"
    try:
        slide.notes_slide.notes_text_frame.text = notes_for([(1,)], meta.get("title", ""))
    except Exception:
        pass

    # 2. Overview / abstract
    abstract = meta.get("abstract", "")
    _add_text_slide(
        prs, "Overview / Abstract", abstract or "(no abstract detected)",
        notes_for([(1,)], abstract or "(no abstract detected)"),
    )

    # 3. Section slides
    for i, sec in enumerate(sections):
        next_sec = sections[i + 1] if i + 1 < len(sections) else None
        points = _section_key_points(blocks, sec, next_sec)
        body = "\n".join(f"• {p['text'][:220]}" for p in points) or "(empty section)"
        refs = [(p["page"], p["block"]) for p in points] or [(sec["page"], sec.get("block"))]
        src_text = "\n".join(p["text"] for p in points)
        _add_text_slide(prs, sec["heading"], body, notes_for(refs, src_text))

    # 4. Table slides
    for t in parsed.get("tables", []):
        rows = t.get("rows") or []
        if not rows:
            continue
        slide = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only
        slide.shapes.title.text = f"Table {t['id']} (p.{t['page']})"
        ncols = max(len(r) for r in rows) or 1
        nrows = min(len(rows), 20)
        left, top = Inches(0.5), Inches(1.4)
        width, height = Inches(9.0), Inches(0.4) * nrows
        try:
            gfx = slide.shapes.add_table(nrows, ncols, left, top, width, height)
            tbl = gfx.table
            for ri in range(nrows):
                for ci in range(ncols):
                    val = rows[ri][ci] if ci < len(rows[ri]) else ""
                    tbl.cell(ri, ci).text = str(val)[:200]
            slide.notes_slide.notes_text_frame.text = notes_for(
                [(t["page"], t.get("id"))], json.dumps(rows, ensure_ascii=False)[:2000])
        except Exception as exc:
            slide.notes_slide.notes_text_frame.text = f"table render failed: {exc}"
    # 5. Figure slides
    for f in parsed.get("figures", []):
        bbox = f.get("bbox")
        if not bbox:
            continue
        slide = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only
        slide.shapes.title.text = f"Figure {f['id']} (p.{f['page']}) — {f.get('caption', '')[:80]}"
        png_path = figure_dir / f"{f['id']}.png"
        if _render_figure_png(pdf_path, f["page"], bbox, png_path):
            try:
                slide.shapes.add_picture(str(png_path), Inches(0.6), Inches(1.4),
                                         height=Inches(5.0))
            except Exception:
                pass
        try:
            slide.notes_slide.notes_text_frame.text = notes_for(
                [(f["page"], f.get("block"))], f.get("caption", ""))
        except Exception:
            pass

    # 6. Formula slides
    formulas = parsed.get("formulas", [])
    if formulas:
        body = "\n".join(f"({f['id']}, p.{f['page']}) {f['text'][:160]}" for f in formulas[:20])
        _add_text_slide(prs, "Formulas & Symbols", body,
                        notes_for([(f["page"], f.get("block")) for f in formulas],
                                  "\n".join(f["text"] for f in formulas)))

    # 7. References slide
    refs = parsed.get("references", [])
    if refs:
        body = "\n".join(r["text"][:180] for r in refs[:40])
        _add_text_slide(prs, f"References ({len(refs)})", body,
                        notes_for([(r["page"], r.get("block")) for r in refs],
                                  "\n".join(r["text"] for r in refs)))

    try:
        prs.save(str(out_path))
    except Exception as exc:
        raise RuntimeError(f"pptx save failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Minimal stdlib OOXML fallback (valid but basic PPTX)
# ---------------------------------------------------------------------------

def _xml_decl() -> str:
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _minimal_slide_xml(title: str, body: str, notes: str, slide_id: int) -> str:
    return (
        _xml_decl() +
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<p:cSld><p:spTree>'
        f'<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="457200" y="274320"/><a:ext cx="8229600" cy="914400"/></a:xfrm></p:spPr>'
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="2800" b="1"/>'
        f'<a:t>{escape(title)}</a:t></a:r></a:p></p:txBody></p:sp>'
        f'<p:sp><p:nvSpPr><p:cNvPr id="3" name="Body"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="457200" y="1371600"/><a:ext cx="8229600" cy="5486400"/></a:xfrm></p:spPr>'
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="1600"/>'
        f'<a:t>{escape(body)}</a:t></a:r></a:p></p:txBody></p:sp>'
        '</p:spTree></p:cSld><p:clrMapOvr><a:overrideClrMapping bg1="lt1" tx1="dk1" '
        'bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" '
        'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/></p:clrMapOvr>'
        '</p:sld>'
    )


def _build_pptx_minimal(parsed: dict, out_path: Path) -> None:
    slides = []
    meta = parsed.get("title_meta", {})
    slides.append(("Title", meta.get("title") or "Untitled", meta.get("abstract") or ""))
    for sec in parsed.get("sections", []):
        points = _section_key_points(parsed.get("blocks", []), sec, None)
        body = "\n".join(f"- {p['text'][:180]}" for p in points[:4])
        slides.append((sec["heading"], body, body))
    if not slides:
        slides.append(("Empty", "No content parsed", ""))

    # Build a minimal-but-openable PPTX package (zip of OOXML parts).
    content_types = (
        _xml_decl() +
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '</Types>'
    )
    rels = (
        _xml_decl() +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
        '</Relationships>'
    )
    slide_ids = "".join(
        f'<p:sldId id="{256 + i}" r:id="rId{1 + i}"/>' for i in range(len(slides))
    )
    pres_rels = _xml_decl() + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    pres_rels += "".join(
        f'<Relationship Id="rId{1 + i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i + 1}.xml"/>'
        for i in range(len(slides)))
    pres_rels += '</Relationships>'
    presentation = (
        _xml_decl() +
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f'<p:sldIdLst>{slide_ids}</p:sldIdLst>'
        '<p:sldSz cx="9144000" cy="6858000" type="screen4x3"/>'
        '<p:notesSz cx="6858000" cy="9144000"/>'
        '</p:presentation>'
    )
    with zipfile.ZipFile(str(out_path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("ppt/presentation.xml", presentation)
        zf.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        for i, (title, body, notes) in enumerate(slides, start=1):
            zf.writestr(f"ppt/slides/slide{i}.xml", _minimal_slide_xml(title, body, notes, i))


# ---------------------------------------------------------------------------
# Outline / speaker notes / evidence map
# ---------------------------------------------------------------------------

def _build_outline(parsed: dict, out_path: Path) -> None:
    lines = ["# Paper slide outline (page-level evidence)\n",
             f"- Source: {parsed['source'].get('file', 'unknown')}",
             f"- Pages: {parsed['source'].get('pages', '?')}\n"]
    meta = parsed.get("title_meta", {})
    if meta.get("title"):
        lines.append(f"## 1. Title — {meta['title']}  (p.1)")
    lines.append("## 2. Overview / Abstract  (p.1)")
    for sec in parsed.get("sections", []):
        lines.append(f"## {sec['heading']}  (p.{sec['page']})")
    for t in parsed.get("tables", []):
        lines.append(f"## Table {t['id']}  (p.{t['page']})")
    for f in parsed.get("figures", []):
        lines.append(f"## Figure {f['id']}  (p.{f['page']}) — {f.get('caption','')[:100]}")
    if parsed.get("formulas"):
        lines.append(f"## Formulas  ({len(parsed['formulas'])} items)")
    if parsed.get("references"):
        lines.append(f"## References  ({len(parsed['references'])} items)")
    try:
        out_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def _build_evidence_map(parsed: dict, out_path: Path) -> None:
    emap = {"source": parsed["source"].get("file", ""), "slides": []}
    for sec in parsed.get("sections", []):
        emap["slides"].append({"slide": sec["heading"], "page": sec["page"],
                               "block": sec.get("block"), "kind": "section"})
    for t in parsed.get("tables", []):
        emap["slides"].append({"slide": f"Table {t['id']}", "page": t["page"], "kind": "table"})
    for f in parsed.get("figures", []):
        emap["slides"].append({"slide": f"Figure {f['id']}", "page": f["page"],
                               "block": f.get("block"), "kind": "figure"})
    C.write_json(out_path, emap)


def _build_speaker_notes(parsed: dict, out_path: Path) -> None:
    lines = ["# Speaker notes (讲稿 / 备注)\n"]
    meta = parsed.get("title_meta", {})
    lines.append(f"## Title\n{meta.get('title','')}\n")
    lines.append(f"## Abstract\n{meta.get('abstract','')}\n")
    for sec in parsed.get("sections", []):
        points = _section_key_points(parsed.get("blocks", []), sec, None)
        lines.append(f"## {sec['heading']}  [p.{sec['page']}]")
        for p in points:
            lines.append(f"- (p.{p['page']} {p['block']}) {p['text']}")
        lines.append("")
    try:
        out_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    C.ensure_dirs()
    wk_dir, out_dir = C.work_dir(), C.output_dir()
    result = C.load_result()
    parsed = _load_parsed(wk_dir)
    if args.dry_run:
        ok = parsed is not None
        print(json.dumps({"status": "ok" if ok else "missing_parsed",
                          "has_pptx_backend": HAS_PPTX, "has_fitz": HAS_FITZ}))
        return 0

    if parsed is None:
        C.record_error(result, "work/parsed.json missing; run parse_pdf.py first", "build_deck")
        result["status"] = "failed"
        C.write_result(result)
        print(json.dumps({"status": "failed", "reason": "no parsed.json"}))
        return 0

    t0 = time.time()
    figure_dir = out_dir / "figures"
    deck_path = out_dir / "deck.pptx"
    errors = []
    try:
        if HAS_PPTX:
            _build_pptx_rich(parsed, deck_path, figure_dir)
        else:
            _build_pptx_minimal(parsed, deck_path)
            C.record_degradation(result, "python-pptx unavailable; used minimal OOXML fallback", "build_deck")
    except Exception as exc:
        errors.append(f"deck build failed: {exc}")
        try:
            _build_pptx_minimal(parsed, deck_path)
            C.record_degradation(result, f"rich pptx failed, fell back to minimal: {exc}", "build_deck")
        except Exception as exc2:
            errors.append(f"minimal fallback also failed: {exc2}")

    _build_outline(parsed, out_dir / "outline.md")
    _build_speaker_notes(parsed, out_dir / "speaker_notes.md")
    _build_evidence_map(parsed, out_dir / "evidence_map.json")

    artifacts = result.get("artifacts", [])
    for rel, kind in (("deck.pptx", "pptx"), ("outline.md", "markdown"),
                      ("speaker_notes.md", "markdown"), ("evidence_map.json", "json")):
        p = out_dir / rel
        if p.exists():
            artifacts.append({"path": str(p), "kind": kind})
    result["artifacts"] = artifacts
    for e in errors:
        C.record_error(result, e, "build_deck")
    result["runtime"]["elapsed_seconds"] = round(time.time() - t0, 1)
    C.write_result(result)
    print(json.dumps({"status": result["status"], "deck": str(deck_path),
                      "pptx_backend": "python-pptx" if HAS_PPTX else "minimal-ooXML",
                      "errors": errors}, ensure_ascii=False))
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
            C.record_error(r, f"{type(exc).__name__}: {exc}", "build_deck.crash")
            C.write_result(r)
        except Exception:
            pass
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(0)
