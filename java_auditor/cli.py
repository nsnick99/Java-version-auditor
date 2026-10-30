"""Command-line entry point."""

from __future__ import annotations

import json
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


def _configure_runtime(deep, offline):
    """Configure bounded network/tool work and disable slow fallbacks in fast mode."""
    import java_auditor.dependency_resolver as dependency_resolver
    import java_auditor.version_checker as version_checker

    dependency_resolver.TOOL_TIMEOUT = TOOL_TIMEOUT if deep else min(20, TOOL_TIMEOUT)
    version_checker.HTTP_TIMEOUT = HTTP_TIMEOUT if deep else min(2, HTTP_TIMEOUT)

    if offline:
        version_checker.fetch_metadata_for = lambda coords: {}

    return dependency_resolver


def main():
    try:
        path, target, deep, offline = _parse_args(sys.argv[1:])
    except ValueError as exc:
        print(f"Argument error: {exc}", file=sys.stderr)
        print(USAGE)
        raise SystemExit(2)

    try:
        _configure_runtime(deep, offline)
        progress(
            f"Starting audit for {path} (target Java {target}; "
            f"{'deep' if deep else 'fast'} mode; {'offline' if offline else 'network enabled'})"
        )
        if path.is_file() and path.suffix.lower() in ARTIFACT_EXTENSIONS:
            import java_auditor.artifact_report as artifact_report
            if offline:
                artifact_report.fetch_metadata_for = lambda coords: {}
            report = artifact_report.build_artifact_report(path, target)
        else:
            import java_auditor.report_builder as report_builder
            if not deep:
                report_builder.resolve_maven_updates = lambda info, root: ({}, None)
            if offline:
                report_builder.fetch_metadata_for = lambda coords: {}
            report = report_builder.build_report(path, target)
    except Exception as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    output = Path(__file__).resolve().parents[1] / "output.json"
    progress(f"Writing report to {output}...")
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(f"Created {output}", flush=True)
