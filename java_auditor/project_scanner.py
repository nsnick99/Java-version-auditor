"""Repository references, missing modules, and selected source API scans."""

from __future__ import annotations

import re

from .config import DEPRECATED_API_RULES
from .utils import iter_project_files, rel

def detect_missing_gradle_modules(settings_files, root):
    missing = []
    include_pattern = re.compile(r"include\s*\(?([^\n)]*)\)?")
    project_dir_pattern = re.compile(r"project\s*\(\s*['\"](:[^'\"]+)['\"]\s*\)\.projectDir\s*=\s*file\(['\"]([^'\"]+)['\"]\)")
    for settings in settings_files:
        text = settings.read_text(encoding="utf-8", errors="replace")
        explicit = {m.group(1): m.group(2) for m in project_dir_pattern.finditer(text)}
        for match in include_pattern.finditer(text):
            for project_path in re.findall(r"['\"](:[^'\"]+)['\"]", match.group(1)):
                configured = explicit.get(project_path)
                folder = (settings.parent / configured).resolve() if configured else (settings.parent / project_path.lstrip(":").replace(":", "/")).resolve()
                if not folder.exists():
                    missing.append({"path": rel(folder, root), "referenced_by": rel(settings, root), "reason": f"Gradle settings include {project_path}, but its project directory does not exist."})
                elif not any((folder / name).exists() for name in ("build.gradle", "build.gradle.kts")):
                    missing.append({"path": rel(folder / "build.gradle", root), "referenced_by": rel(settings, root), "reason": f"Gradle settings include {project_path}, but the project has no build.gradle or build.gradle.kts."})
    return missing


def scan_deprecated_apis(root):
    findings = []
    for path in iter_project_files(root):
        if path.suffix.lower() != ".java":
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, 1):
            for pattern, name, reason in DEPRECATED_API_RULES:
                if pattern.search(line):
                    findings.append({"title": f"Deprecated/legacy API: {name}", "severity": "MEDIUM", "path": rel(path, root), "line": line_no, "evidence": line.strip()[:200], "message": reason})
    return findings
