from __future__ import annotations

import argparse
import json
from pathlib import Path

from auditor import audit


def main():
    parser = argparse.ArgumentParser(
        description="Audit Java versions, dependency updates, known vulnerabilities, and lifecycle warnings."
    )
    parser.add_argument("project", help="Path to the Java project directory")
    parser.add_argument("-o", "--output", default="audit-report.json", help="JSON report path")
    parser.add_argument("--offline", action="store_true", help="Skip online version/security/lifecycle checks")
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="Ask Maven/Gradle to resolve transitive dependencies; executes project build configuration",
    )
    args = parser.parse_args()

    project = Path(args.project).expanduser()
    if not project.exists() or not project.is_dir():
        parser.error("Project path must be an existing directory: " + str(project))

    report = audit(project, online=not args.offline, resolve=args.resolve)
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = report["summary"]
    print("Project: " + str(report["project"]))
    print("Build system: " + str(report["scan"].get("build_system", "Unknown")))
    print("Modules: " + str(summary["modules"]))
    print("Dependencies: " + str(summary["dependencies"]))
    print("Updates available: " + str(summary["updates_available"]))
    print("Technology updates: " + str(summary["technology_updates_available"]))
    print("Vulnerable components: " + str(summary["vulnerable_components"]))
    print("Lifecycle warnings: " + str(summary["lifecycle_warnings"]))
    print("Version conflicts: " + str(summary["version_conflicts"]))
    print(
        "Checks: versions=" + report["checks"]["latest_versions"]
        + ", vulnerabilities=" + report["checks"]["vulnerabilities"]
        + ", lifecycle=" + report["checks"]["lifecycle"]
        + ", dependencies=" + report["scan"]["dependency_resolution"]
    )

    updates = [item for item in report["dependencies"] if item.get("status") == "UPDATE_AVAILABLE"]
    for item in updates[:8]:
        print(
            "- [UPDATE] " + str(item.get("component")) + ": "
            + str(item.get("version")) + " -> " + str(item.get("latest_stable"))
        )
    if len(updates) > 8:
        print("... " + str(len(updates) - 8) + " more updates in the JSON report.")

    technology_updates = [item for item in report["technologies"] if item.get("status") == "UPDATE_AVAILABLE"]
    for item in technology_updates[:4]:
        print(
            "- [UPDATE] " + str(item.get("name")) + " (" + str(item.get("module")) + "): "
            + str(item.get("current_version")) + " -> " + str(item.get("recommended_version"))
        )

    notable = [
        item for item in report["alerts"]
        if item.get("type") in {"KNOWN_VULNERABILITY", "LIFECYCLE_WARNING", "MAINTENANCE_MODE"}
    ]
    for item in notable[:10]:
        component = str(item.get("component", ""))
        current = str(item.get("current_version", ""))
        recommended = item.get("recommended_version")
        line = "- [" + str(item.get("severity", "INFO")) + "] " + str(item.get("type")) + ": " + component
        if current:
            line += " " + current
        if recommended:
            line += " -> " + str(recommended)
        if item.get("advisory"):
            line += " (" + str(item["advisory"]) + ")"
        print(line)
    if len(notable) > 10:
        print("... " + str(len(notable) - 10) + " more alerts in the JSON report.")

    print("Report: " + str(output))


if __name__ == "__main__":
    main()
