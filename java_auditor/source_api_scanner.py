"""Rule-based API scan for a project directory.

Java sources (or, when there are none, compiled .class files) are turned into
lightweight units that expose the same ``class_refs`` / ``member_refs`` as a
parsed class file, so one rule engine (bytecode_scanner) serves both inputs.
"""

from __future__ import annotations

import json
import os
import re
from collections import namedtuple
from pathlib import Path

from .bytecode_scanner import scan_internal_apis, scan_jdk_apis, scan_namespaces
from .classfile import ClassFormatError, parse_class
from .utils import iter_project_files

RULES_DIR = Path(__file__).parent / "rules"
Unit = namedtuple("Unit", "name class_refs member_refs")

_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_FQ = re.compile(r"\b((?:[a-z_]\w*\.)+[A-Z]\w*)")
_STAR_IMPORT = re.compile(r"^\s*import\s+((?:[a-z_]\w*\.)+)\*\s*;", re.M)
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_NEW_WRAPPER = re.compile(r"\bnew\s+(Integer|Long|Double|Boolean|Character|Short|Byte|Float)\s*\(")
_THREAD_STOP = re.compile(r"\b(?:Thread\.currentThread\(\)|[tT]hread)\.(stop|suspend|resume)\s*\(\s*\)")
_FINALIZE = re.compile(r"\bvoid\s+finalize\s*\(\s*\)")
_SIMPLE = {"SecurityManager": "java/lang/SecurityManager"}


def load_rules():
    return {n: json.loads((RULES_DIR / f"{n}.json").read_text(encoding="utf-8"))
            for n in ("jdk_removed", "namespaces")}


def _source_unit(path):
    text = _COMMENTS.sub("", path.read_text(encoding="utf-8", errors="replace"))
    refs = {m.group(1).replace(".", "/") for m in _FQ.finditer(text)}
    refs |= {m.group(1).replace(".", "/") for m in _STAR_IMPORT.finditer(text)}
    refs |= {full for simple, full in _SIMPLE.items() if re.search(rf"\b{simple}\b", text)}
    members = {("java/lang/" + m.group(1), "<init>") for m in _NEW_WRAPPER.finditer(text)}
    members |= {("java/lang/Thread", m.group(1)) for m in _THREAD_STOP.finditer(text)}
    if _FINALIZE.search(text):
        members.add(("java/lang/Object", "finalize"))
    pkg = _PACKAGE.search(text)
    name = (pkg.group(1).replace(".", "/") + "/" if pkg else "") + path.stem
    return Unit(name, refs, members)


def _compiled_units(root):
    units = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules"}]
        for f in files:
            if f.endswith(".class"):
                try:
                    ci = parse_class((Path(base) / f).read_bytes())
                    units.append((f, ci))
                except (ClassFormatError, OSError, IndexError, ValueError):
                    continue
    return units


def scan_project_apis(root, target_java):
    """Return ({deprecatedApis, internalApiUsage, namespaceMigration}, scan_info)."""
    rules = load_rules()
    java_files = [p for p in iter_project_files(root) if p.suffix.lower() == ".java"]
    if java_files:
        units, basis = [(str(p), _source_unit(p)) for p in java_files], "source"
    else:
        units, basis = _compiled_units(root), "bytecode"
    findings = {
        "deprecatedApis": scan_jdk_apis(units, rules["jdk_removed"], target_java),
        "internalApiUsage": scan_internal_apis(units, rules["jdk_removed"]),
        "namespaceMigration": scan_namespaces(units, rules["namespaces"]),
    }
    for group in findings.values():
        group.sort(key=lambda x: ({"BLOCKER": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}[x["severity"]], x["ruleId"]))
    return findings, {"basis": basis, "units_scanned": len(units)}
