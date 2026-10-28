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
    parser.add_argument(
        "--offline", action="store_true",
        help="Skip online version/security/lifecycle checks; with --resolve, force Maven/Gradle into offline mode",
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

    report = audit(project, online=not args.offline, resolve=args.resolve)
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = report["summary"]
    print("Project: " + str(report["project"]))
    print("Build system: " + str(report["scan"].get("build_system", "Unknown")))
    print("Status: " + str(summary["status"]))
    print(summary["message"])
    print("Modules: " + str(summary["modules"]))
    print("Components scanned: " + str(summary["components_scanned"]))
    print("Components OK: " + str(summary["components_ok"]))
    print("Components needing review: " + str(summary["components_needing_review"]))
    print(
        "Checks: versions=" + report["checks"]["latest_versions"]
        + ", vulnerabilities=" + report["checks"]["vulnerabilities"]
        + ", lifecycle=" + report["checks"]["lifecycle"]
    )

    if report["review_items"]:
        print("\nComponents needing attention:")
        for item in report["review_items"]:
            module = item.get("module", ".")
            component = item.get("component", "unknown")
            current = item.get("current_version")
            line = "- " + str(component) + " [" + str(module) + "]"
            if current:
                line += " | current: " + str(current)
            print(line)
            for issue in item.get("issues", []):
                issue_line = "    * " + str(issue.get("type", "REVIEW"))
                if issue.get("recommended_version"):
                    issue_line += " | recommended: " + str(issue["recommended_version"])
                if issue.get("latest_version"):
                    issue_line += " | latest: " + str(issue["latest_version"])
                if issue.get("advisory"):
                    issue_line += " | advisory: " + str(issue["advisory"])
                if issue.get("message"):
                    issue_line += " — " + str(issue["message"])
                if issue.get("details"):
                    issue_line += " | details: " + str(issue["details"])
                print(issue_line)
    else:
        print("\nNo components were flagged for review by the completed checks.")

    print("\nReport: " + str(output))


if __name__ == "__main__":
    main()
