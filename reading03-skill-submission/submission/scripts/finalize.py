#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""finalize.py — 组装双语对照译文、术语表与 16 字段 result.json（无条件导出）。

读取 work/ 下的 segments.json、glossary.json、translations.json、verification.json，
产出：
  output/translation.md  段号 + 原文 + 译文 + 依据（段落级双语对照）
  output/glossary.csv    源词 / 锁定译名 / 状态 / 证据段号 / quote
  output/result.json     16 字段 + output_contract 原样回填 + case_id

状态判定（诚实）：
  ok      coverage_rate == 1.0 且 fail_count == 0
  partial 有译文但未全覆盖或仍有 fail
  failed  无任何译文

无论前置步骤是否成功，本脚本都无条件导出合法 result.json（"finalize 不依赖前序成功"）。

用法：python3 finalize.py [run]
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from pathlib import Path

import _common as C


def _title_from(segments: list) -> str:
    for s in segments[:8]:
        if s.get("type") in ("heading", "text") and len(s["text"]) > 8:
            return s["text"][:200]
    return segments[0]["text"][:200] if segments else ""


def build_translation_md(segments: list, tid_map: dict) -> str:
    lines = ["# 双语对照译文（段落级）", ""]
    pages = sorted({s["page"] for s in segments})
    for page in pages:
        lines.append(f"## 第 {page} 页")
        lines.append("")
        for s in segments:
            if s["page"] != page:
                continue
            t = tid_map.get(s["id"], {})
            zh = (t.get("zh") or "").strip() or "〔待补:未译〕"
            note = (t.get("note") or "").strip()
            basis = ""
            if t.get("basis"):
                basis = f"\n**依据**: {t['basis']}"
            elif t.get("evidence_quote"):
                basis = f"\n**依据**: {t['evidence_quote']}"
            lines.append(f"### {s['id']}（{s['type']}）")
            lines.append(f"**原文**: {s['text']}")
            lines.append(f"**译文**: {zh}")
            if note:
                lines.append(f"**说明**: {note}")
            if basis:
                lines.append(basis)
            lines.append("")
    return "\n".join(lines)


def build_glossary_csv(glossary: list, path: Path) -> str:
    rows = []
    for g in glossary:
        ev = g.get("evidence", {}) or {}
        rows.append({
            "source": g.get("source", ""),
            "target": g.get("target", ""),
            "status": g.get("status", ""),
            "evidence_id": ev.get("id", ""),
            "quote": ev.get("quote", ""),
        })
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["source", "target", "status", "evidence_id", "quote"])
            w.writeheader()
            w.writerows(rows)
    except Exception:
        pass
    return str(path)


def build_evidence(segments: list, tid_map: dict, glossary: list, max_evidence: int = 30) -> list:
    """Verbatim quotes that anchor key results back to the source."""
    evidence = []
    corpus = C.normalize_quote("\n".join(s["text"] for s in segments))
    for s in segments:
        t = tid_map.get(s["id"], {})
        zh = (t.get("zh") or "").strip()
        if not zh or zh.startswith("〔待补") or zh.startswith("〔未译"):
            continue
        q = C.normalize_quote(s["text"])
        if q and q in corpus:
            evidence.append({
                "claim": f"段落 {s['id']} 已译（{len(zh)} 字符）",
                "quote": s["text"][:200],
                "source": "material",
                "locator": {"segment": s["id"], "page": s["page"]},
            })
        if len(evidence) >= max_evidence:
            break
    # glossary locked entries as evidence (no quote -> not locked)
    for g in glossary:
        if g.get("status") == "locked" and g.get("target"):
            ev = g.get("evidence", {}) or {}
            if ev.get("quote"):
                evidence.append({
                    "claim": f"术语 {g['source']} → {g['target']}",
                    "quote": ev["quote"][:200],
                    "source": "material",
                    "locator": {"segment": ev.get("id", ""), "page": ev.get("page", "")},
                })
    return evidence[:max_evidence]


def build_primary_result(segments: list, tid_map: dict, glossary: list, meta: dict, coverage: dict) -> dict:
    unit_index = [
        {"id": s["id"], "page": s["page"], "type": s["type"], "src_chars": s.get("src_chars", len(s["text"]))}
        for s in segments
    ]
    glossary_out = [
        {"src": g.get("source", ""), "tgt": g.get("target", ""), "status": g.get("status", "")}
        for g in glossary
    ]
    return {
        "document": {"title": _title_from(segments), "pages": meta.get("pages", 0) or len({s["page"] for s in segments})},
        "unit_index": unit_index,
        "glossary": glossary_out,
        "coverage": coverage,
        "placeholder_semantics": {
            "not_done": "〔待补:…〕 未做",
            "script_placeholder": "〔未译:…〕 脚本占位",
            "unsure": "〔存疑:…〕 已译但不确信",
        },
    }


def main(argv=None):
    C.ensure_dirs()
    wk_dir, out_dir = C.work_dir(), C.output_dir()
    result = C.load_result()
    t0 = time.time()

    segments_data = C.read_json(wk_dir / "segments.json", {})
    segments = segments_data.get("segments", [])
    meta = segments_data.get("meta", {})
    glossary_data = C.read_json(wk_dir / "glossary.json", {})
    glossary = glossary_data.get("glossary", [])
    translations_data = C.read_json(wk_dir / "translations.json", {})
    translations = translations_data.get("translations", [])
    verification = C.read_json(wk_dir / "verification.json", {})
    coverage = verification.get("coverage", {})
    fail_count = verification.get("fail_count", 0)

    tid_map = {t.get("id"): t for t in translations if isinstance(t, dict)}

    # unconditional outputs
    translation_md = build_translation_md(segments, tid_map)
    md_path = out_dir / "translation.md"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(translation_md, encoding="utf-8")
    except Exception:
        pass
    csv_path = build_glossary_csv(glossary, out_dir / "glossary.csv")

    evidence = build_evidence(segments, tid_map, glossary)
    primary = build_primary_result(segments, tid_map, glossary, meta, coverage)

    # decide status
    units_total = coverage.get("units_total", len(segments))
    units_translated = coverage.get("units_translated", 0)
    coverage_rate = coverage.get("coverage_rate")
    if units_total and units_translated == units_total and fail_count == 0:
        status = "ok"
    elif units_translated > 0:
        status = "partial"
    else:
        status = "failed"

    locked_count = sum(1 for g in glossary if g.get("status") == "locked")
    summary = (
        f"双语对照翻译：{units_translated}/{units_total} 段完成，"
        f"术语表 {len(glossary)} 条（锁定 {locked_count} 条），"
        f"机械核验 fail {fail_count}。覆盖率 {coverage_rate}。"
    )

    # artifacts list
    artifacts = []
    for rel in ("translation.md", "glossary.csv", "extracted_text.md"):
        p = out_dir / rel
        if p.exists():
            artifacts.append({"path": f"outputs/{rel}", "kind": "output"})

    # case_id + output_contract passthrough
    case = C.read_json(C.input_dir() / "case.json", {})
    result["case_id"] = C.find_case_id(case) if case else "unknown"
    contract = C.find_output_contract(case)
    result["output_contract"] = contract if contract else result.get("output_contract", {})

    result["status"] = status
    result["summary"] = summary
    result["artifacts"] = artifacts
    result["primary_result"] = primary
    result["evidence"] = evidence
    result["coverage"] = coverage
    result["verification"] = {
        "checks": verification.get("checks", []),
        "fail_count": fail_count,
        "warn_count": verification.get("warn_count", 0),
    }
    result["runtime"]["elapsed_seconds"] = round(time.time() - t0, 1)
    if status == "partial":
        result.setdefault("uncertainty", []).append(
            "部分段落未完成或有机械核验失败项，详见 verification.failed_segments 与占位符标记。")
    if status == "failed":
        result.setdefault("pending_human", []).append("无有效译文，需人工确认输入材料与翻译执行是否正常。")
    C.write_result(result)

    print(json.dumps({
        "status": status, "units_total": units_total, "units_translated": units_translated,
        "coverage_rate": coverage_rate, "fail_count": fail_count,
        "glossary_locked": locked_count, "evidence": len(evidence),
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
            C.record_error(r, f"{type(exc).__name__}: {exc}", "finalize.crash")
            if r.get("status") in (None, "partial"):
                r["status"] = "failed"
            C.write_result(r)
        except Exception:
            pass
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(0)
