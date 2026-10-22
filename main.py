from __future__ import annotations

import argparse
import json
from pathlib import Path

from auditor import audit


def main():
    parser = argparse.ArgumentParser(
        description="Audit Java technology versions, available dependency updates, lifecycle status, and known vulnerabilities."
    )
    parser.add_argument("project", help="Path to the Java project directory")
    parser.add_argument("-o", "--output", default="audit-report.json", help="JSON report path (default: audit-report.json)")
    parser.add_argument("--offline", action="store_true", help="Skip online release, lifecycle, and vulnerability lookups")
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="Invoke Maven/Gradle to inspect resolved/transitive dependencies; this executes the project's build configuration",
    )
    args = parser.parse_args()

    project = Path(args.project).expanduser()
    if not project.exists() or not project.is_dir():
        parser.error("Project path must be an existing directory: " + str(project))

    report = audit(project, online=not args.offline, resolve=args.resolve)
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("Project: " + report["project"])
    print("Build system: " + report["build_system"])
    print("Modules found: " + str(report["module_count"]))
    print("Components found: " + str(report.get("component_count", 0)))
    summary = report.get("summary", {})
    if args.offline:
        print("Online version/security checks: skipped")
    else:
        print("Available updates: " + str(summary.get("outdated_component_count", 0)))
        print("Known vulnerability findings: " + str(summary.get("vulnerability_finding_count", 0)))
        print("Lifecycle warnings: " + str(summary.get("lifecycle_warning_count", 0)))
        print("Maintenance advisories: " + str(summary.get("maintenance_advisory_count", 0)))
    for module in report["modules"]:
        java_version = module["java"].get("version") or module["java"].get("status")
        boot_version = module["spring_boot"].get("version") or module["spring_boot"].get("status")
        print("- " + module["path"] + ": Java=" + str(java_version) + ", Spring Boot=" + str(boot_version))
    print("Report: " + str(output))


if __name__ == "__main__":
    main()
