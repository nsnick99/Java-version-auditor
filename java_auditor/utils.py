"""Shared path, version, process, and text utilities."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import PLACEHOLDER_START, PRE_RELEASE, SKIP_DIRS

def local_name(node):
    return node.tag.split("}")[-1]


def child(node, name):
    return next((x for x in list(node or []) if local_name(x) == name), None)


def node_text(node, name):
    item = child(node, name)
    return (item.text or "").strip() if item is not None and item.text else None


def clean(value):
    if value is None:
        return None
    value = str(value).strip().strip("'\"")
    return value or None


def interpolate(value, properties):
    if not value:
        return None
    result = value.strip()
    for _ in range(8):
        updated = re.sub(r"\$\{([^}]+)\}", lambda m: properties.get(m.group(1), m.group(0)), result)
        if updated == result:
            break
        result = updated
    return None if PLACEHOLDER_START in result else clean(result)


def java_major(value):
    value = clean(value)
    if not value:
        return None
    if value.startswith("1.") and len(value.split(".")) > 1:
        return value.split(".")[1]
    match = re.match(r"\d+", value)
    return match.group(0) if match else value


def version_tuple(value):
    match = re.match(r"^\s*[vV]?(\d+(?:\.\d+)*)", str(value or ""))
    if not match:
        return ()
    nums = [int(piece) for piece in match.group(1).split(".")]
    return tuple((nums + [0] * 8)[:8])


def compare_versions(left, right):
    a, b = version_tuple(left), version_tuple(right)
    if not a or not b:
        return 0
    if a != b:
        return (a > b) - (a < b)
    left_pre = bool(PRE_RELEASE.search(str(left or "")))
    right_pre = bool(PRE_RELEASE.search(str(right or "")))
    return (right_pre > left_pre) - (left_pre > right_pre)


def version_sort_key(value):
    return version_tuple(value) + (0 if PRE_RELEASE.search(str(value or "")) else 1,)


def rel(path, root):
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/") or "."
    except Exception:
        return str(path).replace("\\", "/")


def should_skip(path, root):
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except Exception:
        parts = path.parts
    return any(part in SKIP_DIRS for part in parts)


def resolve_path_expression(value, basedir, properties=None):
    if not value:
        return None
    value = interpolate(value, properties or {}) or value
    value = value.replace("$" + "{project.basedir}", str(basedir)).replace("$" + "{basedir}", str(basedir))
    if PLACEHOLDER_START in value:
        return None
    path = Path(value)
    return path if path.is_absolute() else basedir / path


def progress(message):
    """Print a flushed progress message so long scans do not look frozen."""
    print(f"[Java Version Auditor] {message}", flush=True)


def iter_project_files(root):
    """Walk project files while pruning generated/vendor directories from traversal."""
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        current_path = Path(current)
        for filename in filenames:
            yield current_path / filename


def find_files(root, names):
    return sorted(
        (p for p in iter_project_files(root) if p.is_file() and p.name in names),
        key=lambda p: str(p).lower(),
    )


def executable_or_wrapper(root, system, parent=None):
    found = shutil.which(system)
    if found:
        return [found]
    search_dirs = [parent, root, *root.parents]
    names = (["mvnw.cmd", "mvnw"] if os.name == "nt" else ["mvnw", "mvnw.cmd"]) if system == "mvn" else (["gradlew.bat", "gradlew"] if os.name == "nt" else ["gradlew", "gradlew.bat"])
    seen = set()
    for folder in search_dirs:
        if folder is None:
            continue
        folder = Path(folder)
        if folder in seen:
            continue
        seen.add(folder)
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                if candidate.suffix.lower() in {".cmd", ".bat"} or os.access(candidate, os.X_OK):
                    return [str(candidate)]
                if candidate.name in {"mvnw", "gradlew"}:
                    return ["sh", str(candidate)]
    return None


def run_command(command, cwd, timeout):
    try:
        if os.name == "nt" and command[0].lower().endswith((".cmd", ".bat")):
            result = subprocess.run(subprocess.list2cmdline(command), cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout, errors="replace")
        else:
            result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, errors="replace")
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout} seconds."
    except Exception as exc:
        return 127, "", str(exc)
