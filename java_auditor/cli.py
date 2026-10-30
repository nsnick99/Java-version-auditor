"""Command-line entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import ARTIFACT_EXTENSIONS, TARGET_JAVA
from .utils import progress

USAGE = 'Usage: python auditor.py "PATH_TO_JAVA_PROJECT_OR_WAR/JAR/EAR" [--target-java N]'


def main():
    args = sys.argv[1:]
    target = TARGET_JAVA
    if len(args) == 3 and args[1] == "--target-java" and args[2].isdigit():
        target, args = int(args[2]), args[:1]
    if len(args) != 1:
        print(USAGE)
        raise SystemExit(2)
    try:
        path = Path(args[0]).expanduser()
        progress(f"Starting audit for {path} (target Java {target})")
        if path.is_file() and path.suffix.lower() in ARTIFACT_EXTENSIONS:
            from .artifact_report import build_artifact_report
            report = build_artifact_report(path, target)
        else:
            from .report_builder import build_report
            report = build_report(path, target)
    except Exception as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    # Always save the report beside the auditor entry point, not in the scanned project.
    output = Path(__file__).resolve().parents[1] / "output.json"
    progress(f"Writing report to {output}...")
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Created {output}", flush=True)
