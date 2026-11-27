#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared utilities for the weekly-skill paper-to-slides pipeline.

Design rules (competition runtime):
  * Pure Python 3 standard library; optional third-party imports are guarded.
  * No import-time side effects: no file reads, no subprocess, no network here
    at module import time. Everything lives in functions.
  * Paths are derived from __file__, never from the current working directory.
  * Every entry point must exit 0 and must still write a valid result.json,
    even on total failure.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ENV_INPUT = "WEEKLY_INPUT_DIR"
_ENV_WORK = "WEEKLY_WORK_DIR"
_ENV_OUTPUT = "WEEKLY_OUTPUT_DIR"

_RESULT_FIELDS = [
    "case_id", "status", "summary", "artifacts", "primary_result",
    "evidence", "sources", "coverage", "verification", "uncertainty",
    "pending_human", "errors", "degradations", "changes", "runtime",
    "output_contract",
]


def skill_root() -> Path:
    """Return the directory that contains SKILL.md.

    The package may be installed at /skills/weekly-skill (runtime), under
    /app/submission (Harbor submission layout), or under any local
    `<root>/submission` or `<root>` directory. Resolve by walking upward from
    this file until SKILL.md is found; fall back to the file's parent-parent.
    """
    here = Path(__file__).resolve()
    for candidate in [here.parent, here.parent.parent, here.parent.parent.parent]:
        if (candidate / "SKILL.md").exists():
            return candidate
    for candidate in [here.parent.parent, here.parent]:
        if candidate.name == "scripts" and (candidate.parent / "SKILL.md").exists():
            return candidate.parent
    return here.parent.parent


def input_dir() -> Path:
    return Path(os.environ.get(_ENV_INPUT, "/input"))


def work_dir() -> Path:
    return Path(os.environ.get(_ENV_WORK, "/app/work"))


def output_dir() -> Path:
    return Path(os.environ.get(_ENV_OUTPUT, "/app/output"))


def ensure_dirs() -> None:
    for d in (work_dir(), output_dir()):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Result skeleton
# ---------------------------------------------------------------------------

def result_skeleton() -> dict:
    return {
        "case_id": "unknown",
        "status": "partial",
        "summary": "",
        "artifacts": [],
        "primary_result": {},
        "evidence": [],
        "sources": [],
        "coverage": {},
        "verification": {"checks": [], "fail_count": 0, "warn_count": 0},
        "uncertainty": [],
        "pending_human": [],
        "errors": [],
        "degradations": [],
        "changes": [],
        "runtime": {"requests": 0, "elapsed_seconds": 0, "script_versions": {}},
        "output_contract": {},
    }


def load_result() -> dict:
    path = output_dir() / "result.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            # Ensure all 16 keys exist, but preserve values already written by
            # earlier pipeline steps (artifacts, evidence, ...).
            skeleton = result_skeleton()
            for key in _RESULT_FIELDS:
                if key in data:
                    skeleton[key] = data[key]
            return skeleton
    except Exception:
        pass
    return result_skeleton()


def write_result(result: dict) -> Path:
    """Write result.json atomically; never raises."""
    ensure_dirs()
    path = output_dir() / "result.json"
    skeleton = result_skeleton()
    if isinstance(result, dict):
        for key in _RESULT_FIELDS:
            if key in result:
                skeleton[key] = result[key]
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(skeleton, fh, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(skeleton, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return path


def record_error(result: dict, message: str, where: str = "pipeline") -> dict:
    result.setdefault("errors", [])
    result["errors"].append({"where": where, "message": message, "at": _now()})
    return result


def record_degradation(result: dict, message: str, where: str = "pipeline") -> dict:
    result.setdefault("degradations", [])
    result["degradations"].append({"where": where, "message": message, "at": _now()})
    return result


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# JSON / text helpers
# ---------------------------------------------------------------------------

def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path: Path, obj) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def read_text(path: Path, limit: int | None = None) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read() if limit is None else fh.read(limit)
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
# Subprocess / tool discovery
# ---------------------------------------------------------------------------

def run_cmd(argv, timeout: int = 120):
    """Run a command; return (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0] if argv else ''}"
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"{type(exc).__name__}: {exc}"


def find_tool(*names: str) -> str | None:
    for name in names:
        if not name:
            continue
        rc, out, _ = run_cmd(["sh", "-c", f"command -v {name}"], timeout=10)
        if rc == 0 and out.strip():
            return out.strip().splitlines()[0]
        # Fall back to a direct which search over common prefixes.
        for base in ("/usr/bin", "/usr/local/bin", "/bin", "/opt/bin"):
            p = Path(base) / name
            if p.exists():
                return str(p)
    return None


# ---------------------------------------------------------------------------
# Input discovery
# ---------------------------------------------------------------------------

_PDF_MAGIC = b"%PDF-"


def discover_inputs(root: Path) -> dict:
    """Recursively list input files and classify them.

    Returns {"files": [...], "pdfs": [...], "case_json": [...], "task_md": [...]}
    Never raises.
    """
    out = {"files": [], "pdfs": [], "case_json": [], "task_md": []}
    try:
        root = Path(root)
        if not root.exists():
            return out
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name in (".DS_Store",) or "__pycache__" in str(path):
                continue
            rel = str(path)
            out["files"].append(rel)
            name = path.name.lower()
            if name == "case.json" or name.endswith(".case.json"):
                out["case_json"].append(rel)
            if name == "task.md" or name.endswith(".task.md"):
                out["task_md"].append(rel)
            try:
                with open(path, "rb") as fh:
                    head = fh.read(16)
                if head.startswith(_PDF_MAGIC):
                    out["pdfs"].append(rel)
            except Exception:
                pass
    except Exception:
        pass
    return out


def find_output_contract(case: dict):
    """Recursively find an output_contract / output_schema / contract object.

    Returns the first dict found, key-normalised; else None.
    """
    if isinstance(case, dict):
        for key, value in case.items():
            norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if norm in ("outputcontract", "outputschema", "contract") and isinstance(value, dict):
                return value
        for value in case.values():
            found = find_output_contract(value)
            if found is not None:
                return found
    elif isinstance(case, list):
        for item in case:
            found = find_output_contract(item)
            if found is not None:
                return found
    return None


def find_case_id(case: dict) -> str:
    """Best-effort case_id discovery from the input JSON."""
    if isinstance(case, dict):
        for key in ("case_id", "caseId", "id", "task_id", "taskId"):
            v = case.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for value in case.values():
            found = find_case_id(value)
            if found:
                return found
    elif isinstance(case, list):
        for item in case:
            found = find_case_id(item)
            if found:
                return found
    return "unknown"


def normalize_quote(text: str) -> str:
    """NFKC-normalise, collapse whitespace, restore hyphenated line breaks,
    and unify common quote/dash variants for verifiable substring matching."""
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "")
    t = t.replace("\u2018", "'").replace("\u2019", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    t = t.replace("\u2013", "-").replace("\u2014", "-")
    # A hyphen followed by a line/word break often means a hyphenated word.
    t = re.sub(r"-\s+", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def normalize_quote_corpus(text: str) -> str:
    return normalize_quote(text)
