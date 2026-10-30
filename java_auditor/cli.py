"""Command-line entry point."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .config import ARTIFACT_EXTENSIONS, TARGET_JAVA, TOOL_TIMEOUT, HTTP_TIMEOUT
from .utils import progress

USAGE = (
    'Usage: python auditor.py "PATH_TO_JAVA_PROJECT_OR_WAR/JAR/EAR" '
    '[--target-java N] [--deep] [--offline]'
)


def _parse_args(argv):
    target = TARGET_JAVA
    deep = False
    offline = False
    path_value = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--target-java":
            if index + 1 >= len(argv) or not argv[index + 1].isdigit():
                raise ValueError("--target-java requires a numeric Java version")
            target = int(argv[index + 1])
            index += 2
            continue
        if arg == "--deep":
            deep = True
            index += 1
            continue
        if arg == "--offline":
            offline = True
            index += 1
            continue
        if arg.startswith("--"):
            raise ValueError(f"Unknown option: {arg}")
        if path_value is not None:
            raise ValueError("Only one project or artifact path may be supplied")
        path_value = arg
        index += 1
    if path_value is None:
        raise ValueError("A project or packaged artifact path is required")
    if not 1 <= target <= 99:
        raise ValueError("Target Java version must be between 1 and 99")
    return Path(path_value).expanduser(), target, deep, offline


def _read_internal_maven_url(project_path):
    """Read the repository URL from the repo-root internal-maven config."""
    candidates = []
    if project_path.is_dir():
        candidates.append(project_path / "internal-maven")
    candidates.append(Path(__file__).resolve().parents[1] / "internal-maven")
    for config_path in candidates:
        if not config_path.is_file():
            continue
        text = config_path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"<url>\s*([^<]+?)\s*</url>", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _configure_runtime(deep, offline, project_path):
    """Configure bounded work and route metadata through internal Maven when configured."""
    import java_auditor.dependency_resolver as dependency_resolver
    import java_auditor.version_checker as version_checker

    dependency_resolver.TOOL_TIMEOUT = TOOL_TIMEOUT if deep else min(20, TOOL_TIMEOUT)
    version_checker.HTTP_TIMEOUT = HTTP_TIMEOUT if deep else min(2, HTTP_TIMEOUT)

    internal_url = _read_internal_maven_url(project_path)
    if internal_url and not offline:
        from .internal_maven import maven_metadata
        version_checker.maven_metadata = lambda group, artifact: maven_metadata(group, artifact, internal_url)
        progress(f"Maven metadata source: internal repository {internal_url}")
    elif internal_url and offline:
        progress("Maven metadata source: internal repository configured, offline mode enabled")

    if offline:
        if internal_url:
            from .internal_maven import maven_metadata
            version_checker.maven_metadata = lambda group, artifact: maven_metadata(group, artifact, internal_url)
            version_checker.fetch_metadata_for = _internal_fetch(version_checker, internal_url)
        else:
            version_checker.fetch_metadata_for = lambda coords: {}

    return dependency_resolver


def _internal_fetch(version_checker, internal_url):
    def fetch(coords):
        cache = {}
        for group, artifact in sorted(set(coords)):
            try:
                cache[(group, artifact)] = version_checker.maven_metadata(group, artifact)
            except Exception:
                cache[(group, artifact)] = {"status": "UNAVAILABLE", "versions": []}
        return cache
    return fetch


def _write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    try:
        path, target, deep, offline = _parse_args(sys.argv[1:])
    except ValueError as exc:
        print(f"Argument error: {exc}", file=sys.stderr)
        print(USAGE)
        raise SystemExit(2)

    try:
        _configure_runtime(deep, offline, path)
        progress(
            f"Starting audit for {path} (target Java {target}; "
            f"{'deep' if deep else 'fast'} mode; {'offline' if offline else 'network enabled'})"
        )
        if path.is_file() and path.suffix.lower() in ARTIFACT_EXTENSIONS:
            import java_auditor.artifact_report as artifact_report
            if offline and not _read_internal_maven_url(path):
                artifact_report.fetch_metadata_for = lambda coords: {}
            report = artifact_report.build_artifact_report(path, target)
        else:
            import java_auditor.report_builder as report_builder
            if not deep:
                report_builder.resolve_maven_updates = lambda info, root: ({}, None)
            if offline and not _read_internal_maven_url(path):
                report_builder.fetch_metadata_for = lambda coords: {}
            report = report_builder.build_report(path, target)
    except Exception as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    output_dir = Path(__file__).resolve().parents[1]
    output = output_dir / "output.json"
    progress(f"Writing full report to {output}...")
    _write_json(output, report)

    from .critical_report import build_critical_report
    critical_output = output_dir / "critical_updates.json"
    critical_report = build_critical_report(report)
    progress(f"Writing critical report to {critical_output}...")
    _write_json(critical_output, critical_report)

    print(f"Created {output}", flush=True)
    print(f"Created {critical_output}", flush=True)
