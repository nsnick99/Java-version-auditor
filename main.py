from __future__ import annotations

import argparse
import json
from pathlib import Path

from auditor import audit


def main():
    parser = argparse.ArgumentParser(
        description="Find Java components that need a version, security, lifecycle, or conflict review."
    )
    parser.add_argument("project", help="Path to the Java project directory")
    parser.add_argument("-o", "--output", default="audit-report.json", help="JSON report path")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--internal-only",
        action="store_true",
        help="Query only private/local Maven repositories declared in pom.xml; skip public lifecycle APIs",
    )
    mode_group.add_argument(
        "--offline",
        action="store_true",
        help="Make no network requests; perform static checks only",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="Ask Maven/Gradle to resolve transitive dependencies; executes project build configuration",
    )
    args = parser.parse_args()

    project = Path(args.project).expanduser()
    if not project.exists() or not project.is_dir():
        parser.error("Project path must be an existing directory: " + str(project))

    try:
        report = audit(
            project,
            resolve=args.resolve,
            internal_only=args.internal_only,
            offline=args.offline,
        )
    except ValueError as error:
        parser.error(str(error))
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = report["summary"]
    checks = report.get("checks", {})
    project_info = report.get("project", {})
    project_name = project_info.get("name", "Unknown") if isinstance(project_info, dict) else project_info
    print("Project: " + str(project_name))
    print("Status: " + str(summary.get("status", "UNKNOWN")))
    print(summary.get("message", ""))
    print("Total dependencies: " + str(summary.get("total_dependencies", 0)))
    print("Outdated components: " + str(summary.get("outdated_components", 0)))
    print("Outdated technologies: " + str(summary.get("outdated_technologies", 0)))
    print("Components needing review: " + str(summary.get("components_needing_review", 0)))
    print("Checks: mode=" + str(checks.get("mode", "UNKNOWN"))
          + ", latest_versions=" + str(checks.get("latest_versions", "UNKNOWN"))
          + ", lifecycle=" + str(checks.get("lifecycle", "UNKNOWN")))

    if report.get("outdated_components"):
        print("\nComponents needing attention:")
        for item in report["outdated_components"]:
            print("- " + str(item.get("component", item.get("name", "unknown"))))
            print("  Current: " + str(item.get("current_version")))
            print("  Latest / alternative: " + str(item.get("latest_version")))
            print("  Status: " + str(item.get("status")))
            print("  Reason: " + str(item.get("reason")))
            print("  Action: " + str(item.get("action")))
    if report.get("outdated_technologies"):
        print("\nTechnologies needing attention:")
        for item in report["outdated_technologies"]:
            print("- " + str(item.get("component", item.get("name", "unknown"))))
            print("  Current: " + str(item.get("current_version")))
            print("  Latest: " + str(item.get("latest_version")))
            print("  Status: " + str(item.get("status")))
            print("  Reason: " + str(item.get("reason")))
            print("  Action: " + str(item.get("action")))
    if report.get("review_items"):
        print("\nChecks needing review:")
        for item in report["review_items"]:
            print("- " + str(item.get("component", item.get("name", "unknown")))
                  + " | " + str(item.get("status", "REVIEW"))
                  + " | " + str(item.get("reason", "")))
    print("\nReport saved to: " + str(output))

if __name__ == "__main__":
    main()
