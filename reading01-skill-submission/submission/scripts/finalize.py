#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Assemble the final result.json (16 fields) from the parsed structure.

Verifies that every evidence quote is verbatim-searchable in the extracted
text, computes coverage and a honest status, and passes the input
output_contract through unchanged.

Usage:
  python3 finalize.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import _common as C


def _load_case(in_dir: Path) -> dict:
    disc = C.discover_inputs(in_dir)
    if disc["case_json"]:
        return C.read_json(in_dir / disc["case_json"][0], {}) or {}
    return {}


def _match_quote(quote: str, corpus: str) -> bool:
    if not quote or not corpus:
        return False
    return C.normalize_quote(quote) in C.normalize_quote_corpus(corpus)


def _build_primary_result(parsed: dict, out_dir: Path) -> dict:
    meta = parsed.get("title_meta", {})
    document = {
        "title": meta.get("title", ""),
        "pages": parsed.get("source", {}).get("pages", 0),
        "sections": [{"heading": s["heading"], "page": s["page"], "level": s.get("level", 1)}
                     for s in parsed.get("sections", [])],
        "tables": [{"id": t["id"], "page": t["page"], "caption": t.get("caption", "")}
                   for t in parsed.get("tables", [])],
        "figures": [{"id": f["id"], "page": f["page"], "caption": f.get("caption", "")}
                    for f in parsed.get("figures", [])],
        "formulas": [{"id": e["id"], "page": e["page"], "text": e.get("text", "")[:200]}
                     for e in parsed.get("formulas", [])],
        "references": [{"id": r["id"], "page": r["page"], "text": r.get("text", "")[:200]}
                       for r in parsed.get("references", [])],
    }
    return {
        "document": document,
        "extracted_text_path": str(out_dir / "extracted_text.md"),
        "deck_path": str(out_dir / "deck.pptx"),
        "evidence_map_path": str(out_dir / "evidence_map.json"),
    }


def _build_evidence(parsed: dict, corpus: str) -> list:
    ev = []
    for s in parsed.get("sections", []):
        quote = s["heading"]
        ev.append({
            "id": f"E{len(ev)+1}", "claim": f"章节：{quote}",
            "quote": quote, "locator": f"paper p.{s['page']} [{s.get('block','')}]",
            "kind": "direct", "verified": _match_quote(quote, corpus),
        })
    for t in parsed.get("tables", []):
        rows = t.get("rows") or []
        # Prefer the caption, then a joined header/first row, so the quote is a
        # real string from the paper rather than a synthetic id.
        if t.get("caption"):
            quote = t["caption"]
        elif rows and any(str(c).strip() for c in (rows[0] or [])):
            quote = " ".join(str(c) for c in rows[0] if str(c).strip())[:160]
        else:
            quote = f"Table {t['id']}"
        ev.append({
            "id": f"E{len(ev)+1}", "claim": f"表格：{t['id']} (p.{t['page']})",
            "quote": quote, "locator": f"paper p.{t['page']} table {t['id']}",
            "kind": "derived", "verified": _match_quote(quote, corpus),
        })
    for f in parsed.get("figures", []):
        quote = (f.get("caption") or f"Figure {f['id']}")
        ev.append({
            "id": f"E{len(ev)+1}", "claim": f"图注：{quote}",
            "quote": quote, "locator": f"paper p.{f['page']} figure {f['id']}",
            "kind": "direct", "verified": _match_quote(quote, corpus),
        })
    for e in parsed.get("formulas", [])[:30]:
        quote = e.get("text", "")
        ev.append({
            "id": f"E{len(ev)+1}", "claim": f"公式：{quote[:60]}",
            "quote": quote, "locator": f"paper p.{e['page']} [{e.get('block','')}]",
            "kind": "direct", "verified": _match_quote(quote, corpus),
        })
    return ev


def _build_coverage(parsed: dict) -> dict:
    required = ["标题", "摘要", "章节", "表格", "图表", "公式", "参考文献", "页级定位"]
    have = []
    meta = parsed.get("title_meta", {})
    if meta.get("title"):
        have.append("标题")
    if meta.get("abstract"):
        have.append("摘要")
    if parsed.get("sections"):
        have.append("章节")
    if parsed.get("tables"):
        have.append("表格")
    if parsed.get("figures"):
        have.append("图表")
    if parsed.get("formulas"):
        have.append("公式")
    if parsed.get("references"):
        have.append("参考文献")
    if parsed.get("blocks"):
        have.append("页级定位")
    missing = [r for r in required if r not in have]
    done = len(have)
    return {"required": len(required), "done": done,
            "rate": round(done / len(required), 3) if required else 0,
            "missing": missing, "notes": ""}


def main():
    C.ensure_dirs()
    in_dir, wk_dir, out_dir = C.input_dir(), C.work_dir(), C.output_dir()
    result = C.load_result()
    t0 = time.time()

    case = _load_case(in_dir)
    parsed = C.read_json(wk_dir / "parsed.json")
    if parsed is None:
        C.record_error(result, "work/parsed.json missing", "finalize")
        result["status"] = "failed"
        result["summary"] = "No parsed structure available."
        C.write_result(result)
        print(json.dumps({"status": "failed", "reason": "no parsed.json"}))
        return 0

    case_id = C.find_case_id(case)
    if case_id == "unknown":
        case_id = Path(in_dir).name or "unknown"
    contract = C.find_output_contract(case) or {}

    result["case_id"] = case_id
    result["output_contract"] = contract
    result["primary_result"] = _build_primary_result(parsed, out_dir)

    corpus = C.read_text(out_dir / "extracted_text.md")
    evidence = _build_evidence(parsed, corpus)
    result["evidence"] = evidence

    # sources
    src = parsed.get("source", {})
    result["sources"] = [{
        "id": "S1", "file": src.get("file", ""), "kind": "pdf",
        "pages": src.get("pages", 0), "readable": bool(parsed.get("blocks")),
        "text_layer": src.get("text_layer", False),
    }]

    result["coverage"] = _build_coverage(parsed)

    # verification
    checks = []
    total_ev = len(evidence)
    verified = sum(1 for e in evidence if e.get("verified"))
    checks.append({"id": "C1", "name": "quote 逐字匹配", "checked": total_ev,
                   "passed": verified, "failed": total_ev - verified})
    files = [out_dir / "deck.pptx", out_dir / "outline.md",
             out_dir / "speaker_notes.md", out_dir / "evidence_map.json"]
    present = [f for f in files if f.exists()]
    checks.append({"id": "C2", "name": "主产物存在", "checked": len(files),
                   "passed": len(present), "failed": len(files) - len(present)})
    fail_count = sum(c["failed"] for c in checks)
    result["verification"] = {"checks": checks, "fail_count": fail_count, "warn_count": 0}

    # honest status
    errors = result.get("errors", [])
    degradations = result.get("degradations", [])
    if not parsed.get("blocks"):
        status = "failed"
    elif errors or fail_count > 0 or result["coverage"]["rate"] < 0.7:
        status = "partial"
    else:
        status = "ok"
    result["status"] = status

    if not result.get("summary"):
        result["summary"] = (
            f"将论文解析为 {result['coverage']['done']}/{result['coverage']['required']} 类结构，"
            f"产出可编辑 PPT 与渲染预览，证据核验 {verified}/{total_ev} 条逐字匹配。"
        )

    # uncertainty: unverified evidence
    result["uncertainty"] = [
        {"id": e["id"], "reason": "quote 未在提取文本中逐字匹配，需人工复核", "locator": e["locator"]}
        for e in evidence if not e.get("verified")
    ]

    # pending_human for missing structures
    for m in result["coverage"].get("missing", []):
        result.setdefault("pending_human", []).append(
            {"item": m, "action": f"检查原文是否包含「{m}」；若缺失，在 notes 中说明"})

    # runtime
    result["runtime"]["elapsed_seconds"] = round(time.time() - t0, 1)
    result["runtime"]["script_versions"] = {
        "_common": "1.0", "parse_pdf": "1.0", "build_deck": "1.0",
        "render_preview": "1.0", "finalize": "1.0",
    }

    C.write_result(result)
    print(json.dumps({"status": status, "case_id": case_id, "evidence": total_ev,
                      "verified": verified, "coverage": result["coverage"]["rate"],
                      "fail_count": fail_count}, ensure_ascii=False))
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
            C.record_error(r, f"{type(exc).__name__}: {exc}", "finalize.crash")
            r["status"] = "failed"
            C.write_result(r)
        except Exception:
            pass
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(0)
