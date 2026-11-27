#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the editable deck to real PDF and PNG previews for inspection.

Ladder:
  1. LibreOffice/soffice --headless --convert-to pdf  (deck.pptx -> deck.pdf)
  2. pdftoppm -png  (deck.pdf -> previews/page-XX.png)

If any renderer is missing, the degradation is recorded honestly in
result.json and the editable PPTX is still delivered.

Usage:
  python3 render_preview.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import _common as C


def _render_pptx_to_pdf(pptx: Path, out_dir: Path, soffice: str | None) -> str | None:
    if not soffice:
        return None
    rc, out, err = C.run_cmd(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(pptx)],
        timeout=300,
    )
    if rc != 0:
        return f"libreoffice convert failed rc={rc}: {err[:200]}"
    pdf = out_dir / (pptx.stem + ".pdf")
    return None if pdf.exists() else "pdf not produced by libreoffice"


def _render_pdf_to_png(pdf: Path, out_dir: Path, pdftoppm: str | None) -> str | None:
    if not pdftoppm:
        return None
    preview_dir = out_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = C.run_cmd(
        [pdftoppm, "-png", "-r", "96", "-f", "1", "-l", "8", str(pdf),
         str(preview_dir / "page")],
        timeout=300,
    )
    if rc != 0:
        return f"pdftoppm failed rc={rc}: {err[:200]}"
    return None


def main():
    C.ensure_dirs()
    out_dir = C.output_dir()
    result = C.load_result()
    t0 = time.time()

    pptx = out_dir / "deck.pptx"
    if not pptx.exists():
        C.record_error(result, "deck.pptx missing; run build_deck.py first", "render")
        result["status"] = "failed"
        C.write_result(result)
        print(json.dumps({"status": "failed", "reason": "no deck.pptx"}))
        return 0

    soffice = C.find_tool("soffice", "libreoffice")
    pdftoppm = C.find_tool("pdftoppm")

    errors = []
    err = _render_pptx_to_pdf(pptx, out_dir, soffice)
    if err:
        errors.append(err)
        C.record_degradation(result, err, "render")
    else:
        pdf = out_dir / "deck.pdf"
        err2 = _render_pdf_to_png(pdf, out_dir, pdftoppm)
        if err2:
            errors.append(err2)
            C.record_degradation(result, err2, "render")

    artifacts = result.get("artifacts", [])
    for rel, kind in (("deck.pdf", "pdf"),):
        p = out_dir / rel
        if p.exists():
            artifacts.append({"path": str(p), "kind": kind})
    preview_dir = out_dir / "previews"
    if preview_dir.exists():
        for png in sorted(preview_dir.glob("page-*.png")):
            artifacts.append({"path": str(png), "kind": "png-preview"})
    result["artifacts"] = artifacts
    result["runtime"]["elapsed_seconds"] = round(time.time() - t0, 1)
    C.write_result(result)
    print(json.dumps({"status": result["status"], "deck_pdf": str(out_dir / "deck.pdf"),
                      "previews": len(list((out_dir / "previews").glob("*.png"))) if (out_dir / "previews").exists() else 0,
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
            C.record_error(r, f"{type(exc).__name__}: {exc}", "render.crash")
            C.write_result(r)
        except Exception:
            pass
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(0)
