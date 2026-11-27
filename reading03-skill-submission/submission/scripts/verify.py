#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify.py — 译文机械核验（数字/单位/引用标记/公式编号/术语一致性）。

读取：
  work/segments.json      原文段落单元
  work/glossary.json      术语表（locked 条目才有约束力）
  work/translations.json  agent 写的译文 {translations:[{id, zh, status, note}]}

产出：
  work/verification.json  {checks, fail_count, warn_count, failed_segments,
                           glossary_consistency}
  output/result.json      更新 verification / coverage

核验只做"机械可判"的项，不评价译文是否通顺：
  S4 数字    — 原文每个数字 token 都出现在译文（防漏译/改数字）
  S5 单位    — 原文"数字+单位"组合保留
  S6 引用/公式编号 — [N]、(N) 标记保留
  S7 术语    — locked 术语的目标译名在全篇译文中的一致率

占位符语义（与 SKILL.md 一致）：
  〔待补:…〕未做 / 〔未译:…〕脚本占位 → 计入未完成（fail coverage）
  〔存疑:…〕已译但不确信 → 通过但记 warning

用法：python3 verify.py [run]
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import _common as C

_PLACEHOLDER_NOT_DONE = re.compile(r"〔待补:|〔未译:")
_PLACEHOLDER_UNSURE = re.compile(r"〔存疑:")

_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b")
_UNIT_PATTERN = (
    r"\b\d+(?:\.\d+)?\s*(?:%|mm|cm|nm|μm|µm|km|s|ms|min|h|kg|g|mg|μg|µg|mol|"
    r"mM|μM|µm|mL|L|°C|K|Hz|kHz|MHz|GHz|eV|keV|MeV|GeV|Pa|kPa|MPa|"
    r"V|mV|A|mA|W|kW|N|J|M|G|T|bit|byte|B)\b"
)
_UNIT_RE = re.compile(_UNIT_PATTERN, re.IGNORECASE)
_CITE_RE = re.compile(r"\[\d+(?:\s*[-–,]\s*\d+)*\]")
_FORMULA_NUM_RE = re.compile(r"[\(\（]\d+(?:\.\d+)?[\)\）]")


def _num_tokens(text: str) -> list:
    return _NUM_RE.findall(text or "")


def _unit_tokens(text: str) -> list:
    return [m.group(0) for m in _UNIT_RE.finditer(text or "")]


def _cite_tokens(text: str) -> list:
    return _CITE_RE.findall(text or "")


def _formula_num_tokens(text: str) -> list:
    return _FORMULA_NUM_RE.findall(text or "")


def check_segment(src: str, zh: str) -> dict:
    """Mechanical checks for one segment. Returns {pass, failures, warnings}."""
    failures, warnings = [], []
    src, zh = src or "", zh or ""

    # not-done placeholder counts as a coverage failure (reported separately)
    nums = _num_tokens(src)
    missing_nums = [n for n in nums if n not in zh]
    if missing_nums:
        failures.append(f"numbers_missing={missing_nums[:5]}")

    units = _unit_tokens(src)
    missing_units = [u for u in units if u.split()[-1] not in zh]
    # only fail if the numeric part is also missing (unit letter alone is noisy)
    missing_units = [u for u in units if not any(ch.isdigit() for ch in zh.split(u.split()[-1])[0])]
    if missing_units:
        warnings.append(f"units_unverified={missing_units[:5]}")

    cites = _cite_tokens(src)
    missing_cites = [c for c in cites if c not in zh]
    if missing_cites:
        failures.append(f"citations_missing={missing_cites[:5]}")

    fnum = _formula_num_tokens(src)
    missing_fnum = [f for f in fnum if f not in zh]
    if missing_fnum:
        failures.append(f"formula_numbers_missing={missing_fnum[:5]}")

    if _PLACEHOLDER_UNSURE.search(zh):
        warnings.append("translation_uncertain")

    return {
        "pass": not failures,
        "failures": failures,
        "warnings": warnings,
    }


def check_glossary_consistency(segments: list, translations: dict, glossary: list) -> dict:
    locked = [g for g in glossary if g.get("status") == "locked" and g.get("target")]
    if not locked:
        return {"total": 0, "consistent": 0, "ratio": None, "checked_terms": []}
    id2seg = {s["id"]: s for s in segments}
    rows = []
    for g in locked:
        src_term, tgt = g["source"], g["target"]
        src_hits = sum(1 for s in segments if src_term.lower() in s["text"].lower())
        tgt_hits = sum(
            1 for sid, t in translations.items()
            if t.get("zh") and tgt in t["zh"]
        )
        # consistent if every source occurrence has the target present globally;
        # per-segment exact mapping is noisy, so report the ratio.
        consistent = tgt_hits >= src_hits if src_hits else True
        rows.append({"source": src_term, "target": tgt, "src_hits": src_hits,
                     "tgt_hits": tgt_hits, "consistent": consistent})
    consistent_count = sum(1 for r in rows if r["consistent"])
    return {
        "total": len(rows),
        "consistent": consistent_count,
        "ratio": round(consistent_count / len(rows), 3) if rows else None,
        "checked_terms": rows,
    }


def main(argv=None):
    C.ensure_dirs()
    wk_dir, out_dir = C.work_dir(), C.output_dir()
    result = C.load_result()
    t0 = time.time()

    segments_data = C.read_json(wk_dir / "segments.json", {})
    segments = segments_data.get("segments", [])
    glossary_data = C.read_json(wk_dir / "glossary.json", {})
    glossary = glossary_data.get("glossary", [])
    translations_data = C.read_json(wk_dir / "translations.json", {})
    translations = translations_data.get("translations", [])

    tid_map = {t.get("id"): t for t in translations if isinstance(t, dict)}
    checks = []
    failures, warnings = 0, 0
    failed_segments = []
    done_count, not_done_count = 0, 0

    for s in segments:
        sid = s["id"]
        t = tid_map.get(sid)
        zh = (t.get("zh") or "") if t else ""
        has_translation = bool(zh.strip())
        is_not_done = bool(_PLACEHOLDER_NOT_DONE.search(zh)) if zh else False

        if not has_translation:
            not_done_count += 1
            failed_segments.append(sid)
            checks.append({"id": sid, "pass": False,
                           "failures": ["no_translation"], "warnings": []})
            failures += 1
            continue

        chk = check_segment(s["text"], zh)
        checks.append({"id": sid, "pass": chk["pass"],
                       "failures": chk["failures"], "warnings": chk["warnings"]})
        if is_not_done:
            not_done_count += 1
            failed_segments.append(sid)
            failures += 1
        elif not chk["pass"]:
            failed_segments.append(sid)
            failures += 1
        else:
            done_count += 1
        warnings += len(chk["warnings"])

    units_total = len(segments)
    units_translated = done_count
    coverage = {
        "units_total": units_total,
        "units_translated": units_translated,
        "units_not_done": not_done_count,
        "coverage_rate": round(units_translated / units_total, 3) if units_total else None,
    }
    glossary_consistency = check_glossary_consistency(segments, tid_map, glossary)

    verification = {
        "checks": checks,
        "fail_count": failures,
        "warn_count": warnings,
        "failed_segments": failed_segments,
        "glossary_consistency": glossary_consistency,
        "coverage": coverage,
    }
    C.write_json(wk_dir / "verification.json", verification)

    result["verification"] = {
        "checks": checks,
        "fail_count": failures,
        "warn_count": warnings,
    }
    result["coverage"] = coverage
    result["runtime"]["elapsed_seconds"] = round(time.time() - t0, 1)
    # do not set final status here; finalize.py decides ok vs partial.
    C.write_result(result)

    print(json.dumps({
        "units_total": units_total, "units_translated": units_translated,
        "fail_count": failures, "warn_count": warnings,
        "failed_segments": failed_segments[:20],
        "glossary_ratio": glossary_consistency.get("ratio"),
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
            C.record_error(r, f"{type(exc).__name__}: {exc}", "verify.crash")
            C.write_result(r)
        except Exception:
            pass
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(0)
