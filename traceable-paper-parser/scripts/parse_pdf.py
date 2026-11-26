#!/usr/bin/env python3
"""Source-linked research PDF extraction. No network calls or model dependencies."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict

sys.dont_write_bytecode = True
from layout import bbox_union, make_blocks, mark_margins, median, overlap, reading_order

VERSION = "1.0.0"


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def note(doc, code, message, page=None, severity="review"):
    entry = {"code": code, "message": message, "severity": severity}
    if page is not None:
        entry["page_number"] = page
    if entry not in doc["uncertainty"]:
        doc["uncertainty"].append(entry)


def run(command, timeout):
    if timeout <= 0:
        raise TimeoutError("Parsing time budget exhausted")
    result = subprocess.run(command, capture_output=True, timeout=max(0.1, timeout), shell=False)
    if result.returncode:
        # Command arguments can contain paths. Never include passwords or environment values.
        raise RuntimeError(f"{Path(command[0]).name} exited {result.returncode}: " +
                           result.stderr.decode("utf-8", errors="replace")[:800])
    return result.stdout


def select_pages(value, count):
    if not value:
        return list(range(1, count + 1))
    chosen = set()
    for part in value.split(","):
        if not re.fullmatch(r"\d+(?:-\d+)?", part.strip()):
            raise ValueError("Pages must look like 1-3,5; physical pages start at 1")
        bounds = list(map(int, part.strip().split("-")))
        a, b = bounds if len(bounds) == 2 else (bounds[0], bounds[0])
        if not (1 <= a <= b <= count):
            raise ValueError(f"Page range {part} is outside 1..{count}")
        chosen.update(range(a, b + 1))
    return sorted(chosen)


def new_page(number, width, height, **details):
    return {"page_number": number, "page_index": number - 1, "page_label": None,
            "width": round(width, 3), "height": round(height, 3), "original_rotation": 0,
            "coordinate_system": "unrotated_cropbox_top_left_pdf_points",
            "lines": [], "tables": [], "images": [], "blocks": [], **details}


def span_text(spans):
    raw = "".join(s["text"] for s in spans)
    parts = []
    inserted = False
    previous = None
    for s in spans:
        if previous and previous["text"] and s["text"]:
            gap = s["bbox"][0] - previous["bbox"][2]
            same_baseline = abs(s.get("origin", [0, 0])[1] - previous.get("origin", [0, 0])[1]) < max(2, s["size"] * 0.4)
            if gap > max(2, min(s["size"], previous["size"]) * 0.3) and same_baseline and not (previous["text"][-1].isspace() or s["text"][0].isspace()):
                parts.append(" ")
                inserted = True
        parts.append(s["text"])
        previous = s
    return raw, "".join(parts), inserted


def native_lines(page, number, fitz):
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    data = page.get_text("dict", flags=flags, sort=False)
    lines = []
    for bi, block in enumerate(data.get("blocks", [])):
        for li, source in enumerate(block.get("lines", [])):
            spans = []
            for s in source.get("spans", []):
                if not s.get("text"):
                    continue
                spans.append({"text": s["text"], "bbox": [round(float(v), 3) for v in s["bbox"]],
                              "font": s.get("font", ""), "size": round(float(s.get("size", 0)), 3),
                              "flags": int(s.get("flags", 0)), "origin": list(s.get("origin", (0, 0)))})
            if not spans:
                continue
            raw, text, inserted = span_text(spans)
            lines.append({"id": f"p{number:04d}-l{len(lines)+1:04d}", "page_number": number,
                          "bbox": bbox_union(spans), "raw_text": raw, "text": text,
                          "spans": spans, "font_size": median([s["size"] for s in spans]),
                          "origin_block": f"native-{bi}", "origin_line": li,
                          "source_method": "native_text", "spacing_inferred": inserted,
                          "direction": list(source.get("dir", [1, 0])), "role": "unclassified"})
    return lines


def clustered(values, tolerance=1.5):
    groups = []
    for value in sorted(values):
        if groups and abs(value - groups[-1][-1]) <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(g) / len(g) for g in groups]


def native_tables(page, number, out, doc):
    tables = []
    if not hasattr(page, "find_tables"):
        note(doc, "table_backend_unavailable", "This PyMuPDF version has no find_tables; inspect table regions visually.", number)
        return tables
    try:
        detected = page.find_tables()
        for ti, table in enumerate(detected.tables, 1):
            if table.row_count < 2 or table.col_count < 2:
                continue
            identifier = f"p{number:04d}-t{ti:03d}"
            boxes = [list(c) for c in table.cells if c is not None]
            if not boxes:
                continue
            xs = clustered([c[k] for c in boxes for k in (0, 2)])
            ys = clustered([c[k] for c in boxes for k in (1, 3)])
            extracted = table.extract()
            cells = []
            seen = set()
            for ri, row in enumerate(table.rows):
                for ci, box in enumerate(row.cells):
                    if box is None:
                        continue
                    box = [round(float(v), 3) for v in box]
                    key = tuple(box)
                    if key in seen:
                        continue
                    seen.add(key)
                    r0 = min(range(len(ys)), key=lambda k: abs(ys[k] - box[1]))
                    r1 = min(range(len(ys)), key=lambda k: abs(ys[k] - box[3]))
                    c0 = min(range(len(xs)), key=lambda k: abs(xs[k] - box[0]))
                    c1 = min(range(len(xs)), key=lambda k: abs(xs[k] - box[2]))
                    text = extracted[ri][ci] if ri < len(extracted) and ci < len(extracted[ri]) else None
                    if text is None:
                        text = page.get_textbox(box)
                    cells.append({"id": f"{identifier}-r{r0+1}c{c0+1}", "page_number": number,
                                  "row": r0, "column": c0, "rowspan": max(1, r1-r0),
                                  "colspan": max(1, c1-c0), "bbox": box, "text": text,
                                  "source_method": "pymupdf_ruled_table"})
            grid = {"id": identifier, "page_number": number, "bbox": list(table.bbox),
                    "row_count": len(ys)-1, "column_count": len(xs)-1, "cells": cells,
                    "extracted_rows": extracted, "header": None, "caption_id": None,
                    "source_method": "pymupdf_ruled_table", "header_inference_verified": False}
            header = getattr(table, "header", None)
            if header:
                grid["header"] = {"names": list(header.names), "external": bool(header.external),
                                  "bbox": list(header.bbox)}
            grid["html_path"] = f"tables/{identifier}.html"
            (out / "tables").mkdir(exist_ok=True)
            (out / grid["html_path"]).write_text(table_html(grid), encoding="utf-8")
            tables.append(grid)
    except Exception as error:
        note(doc, "table_detection_failed", f"Ruled-table extraction failed: {type(error).__name__}; native text retained.", number)
    return tables


def table_html(table):
    by_start = {(c["row"], c["column"]): c for c in table["cells"]}
    covered = set()
    output = ['<!doctype html><meta charset="utf-8"><style>table{border-collapse:collapse}td{border:1px solid #777;padding:6px;white-space:pre-wrap}</style><table>']
    for r in range(table["row_count"]):
        output.append("<tr>")
        for c in range(table["column_count"]):
            if (r, c) in covered:
                continue
            cell = by_start.get((r, c))
            if cell is None:
                output.append('<td data-state="unresolved"></td>')
                continue
            for rr in range(r, r+cell["rowspan"]):
                for cc in range(c, c+cell["colspan"]):
                    covered.add((rr, cc))
            output.append(f'<td rowspan="{cell["rowspan"]}" colspan="{cell["colspan"]}" data-source="{cell["id"]}">{html.escape(cell["text"] or "")}</td>')
        output.append("</tr>")
    output.append("</table>")
    return "\n".join(output)


def available_ocr(language):
    exe = shutil.which("tesseract")
    if not exe:
        return None, [], language.split("+")
    try:
        listing = run([exe, "--list-langs"], 8).decode("utf-8", errors="replace")
        available = {line.strip() for line in listing.splitlines() if re.fullmatch(r"[\w/]+", line.strip())}
    except Exception:
        return exe, [], language.split("+")
    requested = language.split("+")
    return exe, [v for v in requested if v in available], [v for v in requested if v not in available]


def ocr_lines(image, page, exe, languages, deadline, timeout, doc):
    from_png_width = page["ocr_image_width"]
    from_png_height = page["ocr_image_height"]
    data = run([exe, str(image), "stdout", "-l", "+".join(languages), "--psm", "3", "tsv"], min(timeout, deadline-time.monotonic()))
    words = defaultdict(list)
    for row in csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace")), delimiter="\t"):
        text = row.get("text", "").strip()
        if row.get("level") != "5" or not text:
            continue
        try:
            if float(row["conf"]) < 0:
                continue
            key = tuple(row[k] for k in ("block_num", "par_num", "line_num"))
            x, y, w, h = [float(row[k]) for k in ("left", "top", "width", "height")]
            sx, sy = page["width"]/from_png_width, page["height"]/from_png_height
            words[key].append({"text": text, "bbox": [x*sx, y*sy, (x+w)*sx, (y+h)*sy], "confidence": float(row["conf"])})
        except (ValueError, KeyError, ZeroDivisionError):
            continue
    result = []
    for key, row in words.items():
        row.sort(key=lambda w: w["bbox"][0])
        text = " ".join(w["text"] for w in row)
        confidence = sum(w["confidence"] for w in row)/len(row)
        result.append({"id": "", "page_number": page["page_number"], "bbox": bbox_union(row),
                       "text": text, "raw_text": text, "words": row, "spans": [],
                       "font_size": None, "origin_block": "ocr-"+"-".join(key[:2]),
                       "source_method": "tesseract_tsv", "ocr_confidence": round(confidence, 2),
                       "spacing_inferred": True, "direction": [1, 0], "role": "unclassified"})
    if not result:
        note(doc, "ocr_no_text", "OCR returned no usable text; inspect the page image.", page["page_number"])
    elif any(l["ocr_confidence"] < 65 for l in result):
        note(doc, "ocr_low_confidence", "Some OCR lines have mean word confidence below 65; verify against image.", page["page_number"])
    return result


def combine_ocr(native, recognized):
    combined = list(native)
    removed = 0
    for line in recognized:
        # Prefer the native layer at overlapping regions; never concatenate duplicate text layers.
        duplicates = [n for n in native if overlap(line["bbox"], n["bbox"]) > 0.45 or overlap(n["bbox"], line["bbox"]) > 0.65]
        if duplicates and any("\ufffd" not in n["text"] for n in duplicates):
            removed += 1
        else:
            for n in duplicates:
                n["role"] = "superseded_native"
            combined.append(line)
    return combined, removed


def process_ocr(page, args, doc, image_maker, deadline, ocr):
    chars = sum(sum(c.isalnum() for c in l["text"]) for l in page["lines"])
    replacement = sum(l["text"].count("\ufffd") for l in page["lines"])
    area = page["width"] * page["height"]
    large_images = [i for i in page["images"] if (i["bbox"][2]-i["bbox"][0])*(i["bbox"][3]-i["bbox"][1]) > area*0.4]
    mixed = bool(large_images) and chars < 180
    needs = (chars < 24 and bool(page["images"])) or replacement > max(2, chars*0.02) or mixed
    if not needs and args.ocr != "force":
        return
    if args.ocr == "off":
        note(doc, "ocr_disabled", "An image-dominant or corrupt text region requires OCR or visual transcription; OCR is off.", page["page_number"])
        return
    exe, languages, missing = ocr
    if not exe or not languages:
        note(doc, "ocr_unavailable", "OCR needed but Tesseract or requested language data is unavailable; native text and page preview retained.", page["page_number"])
        return
    if missing:
        note(doc, "ocr_language_missing", "Unavailable OCR languages: " + ", ".join(missing), page["page_number"])
    try:
        image = image_maker(args.dpi, "ocr")
        recognized = ocr_lines(image, page, exe, languages, deadline, args.ocr_timeout, doc)
        page["lines"], duplicates = combine_ocr(page["lines"], recognized)
        page["ocr"] = {"engine": "tesseract", "languages": languages, "dpi": args.dpi,
                       "recognized_line_count": len(recognized), "overlapping_ocr_lines_omitted": duplicates}
        note(doc, "ocr_review_required", "OCR text requires comparison with the page; confidence is not a correctness guarantee.", page["page_number"])
        doc["change_records"].append({"page_number": page["page_number"], "operation": "ocr_layer_added", "overlapping_lines_omitted": duplicates})
    except Exception as error:
        note(doc, "ocr_failed", f"OCR failed ({type(error).__name__}); original native extraction retained.", page["page_number"])


def extract_fitz(path, args, doc, out, deadline, fitz):
    pdf = fitz.open(path)
    try:
        if pdf.needs_pass:
            password = os.environ.get(args.password_env, "")
            if not password or not pdf.authenticate(password):
                raise PermissionError("PDF requires a valid password in the configured password environment variable")
        doc["page_count"] = len(pdf)
        if not len(pdf):
            raise ValueError("PDF contains no pages")
        selected = select_pages(args.pages, len(pdf))
        doc["requested_pages"] = selected
        doc["metadata"] = {k: v for k, v in (pdf.metadata or {}).items() if v}
        doc["backend"] = {"name": "pymupdf", "version": str(fitz.VersionBind)}
        ocr = available_ocr(args.lang) if args.ocr != "off" else (None, [], [])
        for number in selected:
            if time.monotonic() >= deadline:
                note(doc, "budget_exhausted", "Stopped before the next page; remaining pages were not parsed.", number)
                break
            source = pdf[number-1]
            original_rotation = source.rotation
            source.set_rotation(0)  # In-memory coordinate normalization; original input is never saved/changed.
            page = new_page(number, source.rect.width, source.rect.height, original_rotation=original_rotation)
            page["page_label"] = source.get_label() or None
            page["lines"] = native_lines(source, number, fitz)
            for image in source.get_image_info(xrefs=False):
                page["images"].append({"id": f"p{number:04d}-img{len(page['images'])+1:03d}",
                                       "page_number": number, "bbox": list(image["bbox"]),
                                       "width_pixels": image.get("width"), "height_pixels": image.get("height"),
                                       "source_method": "embedded_image_bbox", "caption_id": None})

            def image_maker(dpi, purpose):
                folder = out / ("previews" if purpose == "preview" else "ocr")
                folder.mkdir(exist_ok=True)
                target = folder / f"page-{number:04d}.png"
                pix = source.get_pixmap(dpi=dpi, alpha=False)
                if purpose == "ocr":
                    page["ocr_image_width"], page["ocr_image_height"] = pix.width, pix.height
                pix.save(str(target))
                return target

            process_ocr(page, args, doc, image_maker, deadline, ocr)
            if time.monotonic() < deadline:
                page["tables"] = native_tables(source, number, out, doc)
            doc["pages"].append(page)
            # Save early so interruption preserves an inspectable partial extraction.
            doc["processed_pages"] = [p["page_number"] for p in doc["pages"]]
            write_json(out / "checkpoint.json", {"status": "partial", "document_id": doc["document_id"],
                                                 "processed_pages": doc["processed_pages"], "pages": doc["pages"]})
            complex_page = page["tables"] or page["images"] or any(u.get("page_number") == number for u in doc["uncertainty"])
            # Columns also need inspection; rendering a preview is cheap relative to OCR.
            complex_page = complex_page or len(page["lines"]) > 12
            if args.render != "none" and (args.render == "all" or complex_page) and number in selected[:args.preview_limit] and time.monotonic() < deadline:
                target = image_maker(args.preview_dpi, "preview")
                page["preview_path"] = str(target.relative_to(out)).replace("\\", "/")
    finally:
        pdf.close()


def extract_poppler(path, args, doc, out, deadline):
    exe = shutil.which("pdftotext")
    if not exe:
        raise ModuleNotFoundError("Neither PyMuPDF nor pdftotext is available")
    data = run([exe, "-bbox-layout", "-enc", "UTF-8", str(path), "-"], deadline-time.monotonic())
    # Poppler produces XHTML. ElementTree does not resolve network entities.
    root = ET.fromstring(data)
    elements = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "page"]
    doc["page_count"] = len(elements)
    selected = select_pages(args.pages, len(elements))
    doc["requested_pages"] = selected
    doc["backend"] = {"name": "poppler_bbox_layout", "version": None}
    note(doc, "fallback_backend", "Poppler provides word geometry; font details, table grids and embedded-image enumeration are unavailable.", severity="info")
    ocr = available_ocr(args.lang) if args.ocr != "off" else (None, [], [])
    renderer = shutil.which("pdftoppm")
    for number in selected:
        if time.monotonic() >= deadline:
            note(doc, "budget_exhausted", "Stopped before the next page.", number)
            break
        element = elements[number-1]
        page = new_page(number, float(element.attrib["width"]), float(element.attrib["height"]), coordinate_system="poppler_page_top_left_pdf_points")
        blocks = [e for e in element.iter() if e.tag.rsplit("}", 1)[-1] == "block"]
        for bi, block in enumerate(blocks):
            for line in (e for e in block.iter() if e.tag.rsplit("}", 1)[-1] == "line"):
                words = []
                for word in line:
                    if word.tag.rsplit("}", 1)[-1] != "word":
                        continue
                    words.append({"text": "".join(word.itertext()), "bbox": [float(word.attrib[k]) for k in ("xMin", "yMin", "xMax", "yMax")]})
                if not words:
                    continue
                text = " ".join(w["text"] for w in words)
                page["lines"].append({"id": f"p{number:04d}-l{len(page['lines'])+1:04d}", "page_number": number,
                                      "text": text, "raw_text": text, "bbox": bbox_union(words), "words": words,
                                      "origin_block": f"poppler-{bi}", "spans": [], "font_size": None,
                                      "source_method": "poppler_bbox_layout", "spacing_inferred": True, "role": "unclassified"})
        if not page["lines"]:
            # Without image enumeration, an empty layer could be a scan or a blank page.
            page["images"] = [{"id": f"p{number:04d}-scan-candidate", "page_number": number,
                               "bbox": [0, 0, page["width"], page["height"]], "source_method": "empty_text_layer_candidate"}]
            note(doc, "empty_text_layer", "Empty layer may be blank or scanned; inspect preview.", number)

        def image_maker(dpi, purpose):
            if not renderer:
                raise ModuleNotFoundError("pdftoppm is unavailable")
            folder = out / ("previews" if purpose == "preview" else "ocr")
            folder.mkdir(exist_ok=True)
            target = folder / f"page-{number:04d}"
            run([renderer, "-f", str(number), "-l", str(number), "-singlefile", "-r", str(dpi), "-png", str(path), str(target)], deadline-time.monotonic())
            target = target.with_suffix(".png")
            # PNG IHDR has the dimensions; no Pillow dependency is needed.
            header = target.read_bytes()[:24]
            if purpose == "ocr":
                page["ocr_image_width"] = int.from_bytes(header[16:20], "big")
                page["ocr_image_height"] = int.from_bytes(header[20:24], "big")
            return target

        process_ocr(page, args, doc, image_maker, deadline, ocr)
        if args.render != "none" and renderer and number in selected[:args.preview_limit] and time.monotonic() < deadline:
            try:
                target = image_maker(args.preview_dpi, "preview")
                page["preview_path"] = str(target.relative_to(out)).replace("\\", "/")
            except Exception as error:
                note(doc, "preview_failed", type(error).__name__, number)
        doc["pages"].append(page)


def finish_structure(doc):
    pages = doc["pages"]
    mark_margins(pages)
    body_size = median([l.get("font_size") for p in pages for l in p["lines"] if l.get("role") not in {"header", "footer"}])
    doc["sections"] = []
    doc["equations"] = []
    doc["figures"] = []
    doc["table_continuation_candidates"] = []
    for page in pages:
        for index, line in enumerate(page["lines"], 1):
            # OCR IDs are assigned after native IDs; identifiers are stable for this extraction.
            line["id"] = line["id"] or f"p{page['page_number']:04d}-o{index:04d}"
            for table in page["tables"]:
                if overlap(line["bbox"], table["bbox"]) >= 0.65:
                    line["role"] = "table_text"
                    line["table_id"] = table["id"]
            if "\ufffd" in line["text"]:
                note(doc, "replacement_glyph", "Replacement characters remain in extracted text; consult original image.", page["page_number"])
            if line.get("spacing_inferred"):
                doc["change_records"].append({"page_number": page["page_number"], "anchor_id": line["id"],
                                               "operation": "geometric_spaces_inserted", "raw_text_preserved": True})
            if line.get("direction", [1, 0]) != [1, 0]:
                note(doc, "nonhorizontal_text", "Rotated or vertical text may require manual order verification.", page["page_number"])
        make_blocks(page, body_size)
        for block in page["blocks"]:
            if block["kind"] == "heading":
                doc["sections"].append({k: block[k] for k in ("id", "page_number", "bbox", "text", "level")})
            if block["kind"] == "equation_candidate":
                number = re.search(r"[（(](\d+[a-z]?)[）)]\s*$", block["text"])
                doc["equations"].append({**{k: block[k] for k in ("id", "page_number", "bbox", "text", "line_ids")},
                                         "number": number.group(1) if number else None, "latex": None,
                                         "status": "structural_candidate_requires_review"})
            if block["kind"] == "caption":
                candidates = page["tables"] if block["caption_kind"] == "table" else [i for i in page["images"] if i["source_method"] == "embedded_image_bbox"]
                close = [v for v in candidates if min(abs(v["bbox"][1]-block["bbox"][3]), abs(block["bbox"][1]-v["bbox"][3])) < 100 and
                         min(v["bbox"][2],block["bbox"][2]) > max(v["bbox"][0],block["bbox"][0])]
                target = min(close, key=lambda v: min(abs(v["bbox"][1]-block["bbox"][3]), abs(block["bbox"][1]-v["bbox"][3]))) if close else None
                if target:
                    target["caption_id"] = block["id"]
                    target["label"] = block["label"]
                if block["caption_kind"] == "figure":
                    doc["figures"].append({"id": block["id"]+"-figure", "page_number": page["page_number"],
                                           "caption_id": block["id"], "caption": block["text"],
                                           "bbox": target["bbox"] if target else None,
                                           "image_id": target["id"] if target else None,
                                           "status": "image_caption_link_candidate" if target else "caption_only_requires_visual_review"})
        if page["column_cuts"]:
            doc["change_records"].append({"page_number": page["page_number"], "operation": "column_reading_order",
                                           "cuts": page["column_cuts"], "method": "gutter_zones_with_spanning_barriers"})
        if not page["lines"] and not page["images"]:
            page["page_state"] = "no_text_or_embedded_images_detected"
        else:
            page["page_state"] = "extracted"
    for previous, following in zip(pages, pages[1:]):
        if following["page_number"] != previous["page_number"] + 1:
            continue
        for a in previous["tables"]:
            for b in following["tables"]:
                if a["column_count"] != b["column_count"]:
                    continue
                if a["bbox"][3] < previous["height"] * 0.72 or b["bbox"][1] > following["height"] * 0.3:
                    continue
                repeated = bool(a["extracted_rows"] and b["extracted_rows"] and a["extracted_rows"][0] == b["extracted_rows"][0])
                same_label = a.get("label") is not None and a.get("label") == b.get("label")
                if repeated or same_label:
                    doc["table_continuation_candidates"].append({"table_ids": [a["id"],b["id"]],
                        "evidence": {"repeated_first_row": repeated,"same_caption_label":same_label},
                        "status": "requires_visual_confirmation", "merged": False})


def write_outputs(doc, out):
    anchors = []
    markdown = [f"# {Path(doc['input_path']).name}", "", f"Document: `{doc['document_id']}`", f"Status: `{doc['status']}`", ""]
    for page in doc["pages"]:
        markdown += [f"## Physical page {page['page_number']}", ""]
        if page.get("preview_path"):
            markdown += [f"[Page preview]({page['preview_path']})", ""]
        parts = list(page["blocks"]) + [{**t, "kind": "table", "origin_block": t["id"]} for t in page["tables"]]
        parts, _ = reading_order(parts)
        for block in parts:
            markdown.append(f"<!-- source {doc['document_id']}:{block['id']}; page={page['page_number']}; bbox={block['bbox']} -->")
            if block["kind"] == "table":
                markdown += [f"[Structured table {block['id']}]({block['html_path']})", ""]
                for row in block["extracted_rows"]:
                    markdown.append(" | ".join("[merged/empty/unresolved]" if v is None else v.replace("\n", " / ") for v in row))
                markdown.append("")
            elif block["kind"] == "heading":
                markdown += ["### " + block["text"], ""]
            else:
                markdown += [block["text"], ""]
            if block["kind"] != "table":
                anchors.append({**block, "document_id": doc["document_id"], "source_pdf": doc["input_path"]})
        for line in page["lines"]:
            anchors.append({**line, "document_id": doc["document_id"], "source_pdf": doc["input_path"], "anchor_type": "line"})
        margins = [l for l in page["lines"] if l.get("role") in {"header", "footer"}]
        if margins:
            markdown += ["<details><summary>Header / footer (retained separately)</summary>", ""]
            markdown += [f"- [{l['id']}] {l['text']}" for l in margins]
            markdown += ["", "</details>", ""]
        for table in page["tables"]:
            for cell in table["cells"]:
                anchors.append({**cell, "document_id": doc["document_id"], "source_pdf": doc["input_path"], "anchor_type": "table_cell", "table_id": table["id"]})
    if doc["uncertainty"]:
        markdown += ["## Review notes", ""]
        markdown += [f"- {u['code']}: {u['message']}" + (f" (page {u['page_number']})" if 'page_number' in u else "") for u in doc["uncertainty"]]
    write_json(out/"document.json", doc)
    (out/"document.md").write_text("\n".join(markdown)+"\n", encoding="utf-8")
    (out/"anchors.jsonl").write_text("".join(json.dumps(a, ensure_ascii=False, allow_nan=False)+"\n" for a in anchors), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pages", help="Physical pages, e.g. 1-3,5")
    parser.add_argument("--backend", choices=["auto","pymupdf","poppler"], default="auto")
    parser.add_argument("--ocr", choices=["auto","off","force"], default="auto")
    parser.add_argument("--lang", default="eng+chi_sim")
    parser.add_argument("--dpi", type=int, default=240)
    parser.add_argument("--preview-dpi", type=int, default=120)
    parser.add_argument("--preview-limit", type=int, default=24)
    parser.add_argument("--render", choices=["all","uncertain","none"], default="uncertain")
    parser.add_argument("--budget", type=float, default=180)
    parser.add_argument("--ocr-timeout", type=float, default=45)
    parser.add_argument("--password-env", default="PDF_PASSWORD")
    args = parser.parse_args()
    if args.budget <= 0 or not (72 <= args.dpi <= 600) or not (36 <= args.preview_dpi <= 300) or args.preview_limit < 0:
        parser.error("Budget must be positive; OCR dpi 72..600, preview dpi 36..300, preview limit >=0")
    start = time.monotonic()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    path = args.pdf.resolve()
    doc = {"schema_version": "traceable-paper/v1", "parser_version": VERSION,
           "input_path": str(path), "document_id": None, "sha256": None, "status": "partial",
           "page_count": 0, "requested_pages": [], "processed_pages": [], "pages": [],
           "uncertainty": [], "change_records": [], "backend": None}
    exit_code = 0
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024*1024), b""):
                digest.update(chunk)
        doc["sha256"] = digest.hexdigest()
        doc["document_id"] = digest.hexdigest()[:16]
        old = out/"document.json"
        if old.exists():
            existing = json.loads(old.read_text(encoding="utf-8"))
            if existing.get("sha256") not in (None, doc["sha256"]):
                print("Output directory contains results for another input; choose a different --out", file=sys.stderr)
                return 2
        write_json(out/"checkpoint.json", {"status":"partial", "document_id": doc["document_id"], "processed_pages": []})
        fitz = None
        if args.backend != "poppler":
            try:
                import pymupdf as fitz
            except ImportError:
                try:
                    import fitz
                except ImportError:
                    if args.backend == "pymupdf":
                        raise ModuleNotFoundError("PyMuPDF backend was requested but is unavailable")
        if fitz is not None:
            extract_fitz(path,args,doc,out,start+args.budget,fitz)
        else:
            extract_poppler(path,args,doc,out,start+args.budget)
        finish_structure(doc)
        doc["processed_pages"] = [p["page_number"] for p in doc["pages"]]
        complete = doc["processed_pages"] == doc["requested_pages"]
        doc["coverage"] = {"requested_pages_completed": complete,
                           "complete_document": len(doc["processed_pages"]) == doc["page_count"],
                           "retained_line_count": sum(len(p["lines"]) for p in doc["pages"]),
                           "body_block_count": sum(len(p["blocks"]) for p in doc["pages"]),
                           "table_count": sum(len(p["tables"]) for p in doc["pages"]),
                           "not_processed_pages": sorted(set(range(1,doc["page_count"]+1))-set(doc["processed_pages"]))}
        doc["status"] = "success" if complete and not any(u["severity"]=="review" for u in doc["uncertainty"]) else "partial"
    except (FileNotFoundError, PermissionError, ModuleNotFoundError) as error:
        doc["status"] = "blocked"
        note(doc, type(error).__name__, str(error))
        exit_code = 2
    except Exception as error:
        doc["status"] = "partial" if doc["pages"] else "failed"
        note(doc, "extraction_error", f"{type(error).__name__}: {str(error)[:600]}")
        exit_code = 2
        if doc["pages"]:
            try:
                finish_structure(doc)
            except Exception:
                pass
    if doc["sha256"] is None and (out/"document.json").exists():
        print("Input could not be read; existing output was left intact",file=sys.stderr)
        return 2
    doc["elapsed_seconds"] = round(time.monotonic()-start,3)
    write_outputs(doc,out)
    print(json.dumps({"status":doc["status"], "document_id":doc["document_id"],
                      "processed_pages": [p["page_number"] for p in doc["pages"]],
                      "output": str(out/"document.json"), "review_items":len(doc["uncertainty"])}, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
