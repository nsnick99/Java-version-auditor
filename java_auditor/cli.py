"""Command-line entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .report_builder import build_report
from .utils import progress

def main():
    if len(sys.argv) != 2:
        print('Usage: python auditor.py "PATH_TO_JAVA_PROJECT"')
        raise SystemExit(2)
    try:
        project_path = Path(sys.argv[1]).expanduser()
        progress(f"Starting audit for {project_path}")
        report = build_report(project_path)
    except Exception as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    output = project_path.resolve() / "output.json"
    progress(f"Writing report to {output}...")
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Created {output}", flush=True)
