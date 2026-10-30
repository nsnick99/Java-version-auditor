"""Build a compact, action-focused migration report from the full audit report."""

from __future__ import annotations

from datetime import datetime


# These components can require source/configuration changes when moving across
# major generations. Merely having a newer version available is not enough to
# put a dependency into the critical report.
CRITICAL_COORDINATES = {
    "io.jsonwebtoken:jjwt",
    "jakarta.persistence:jakarta.persistence-api",
    "javax.persistence:javax.persistence-api",
    "javax.servlet:javax.servlet-api",
    "javax.validation:validation-api",
    "jakarta.validation:jakarta.validation-api",
    "jakarta.transaction:jakarta.transaction-api",
    "javax.transaction:javax.transaction-api",
}

CRITICAL_FRAMEWORKS = {
    "Spring Boot",
    "Spring Framework",
    "Spring MVC",
    "Spring Security",
    "JPA",
    "Hibernate",
}


def _dependency_action(dep):
    name = dep["name"]
    if name == "io.jsonwebtoken:jjwt":
        return "Upgrade JJWT and verify/migrate JWT parsing, signing, and claims APIs."
    if "persistence-api" in name:
        return "Align JPA with the selected Spring Boot generation and inspect javax/jakarta imports and persistence APIs."
    if "servlet-api" in name:
        return "Align the Servlet API with the selected Spring Boot generation and inspect javax/jakarta imports."
    if "validation-api" in name:
        return "Align the validation API with the selected Spring Boot generation and inspect javax/jakarta imports."
    if "transaction-api" in name:
        return "Align the transaction API with the selected Spring Boot generation and inspect javax/jakarta imports."
    return "Review compatibility before upgrading; do not blindly select the newest major version."


def build_critical_report(report):
    """Return only actionable build/migration risks from a full report."""
    target_java = str(report.get("target_java", ""))
    current_java = str(report.get("current_java", ""))

    build_blockers = []
    migration_required = []
    critical_dependencies = []
    framework_migrations = []
    api_findings = []

    # Java target is actionable whenever the detected project Java differs from
    # the requested migration target.
    if target_java and current_java not in {"", "Not detected", "Multiple", target_java}:
        build_blockers.append({
            "component": "Java",
            "current_version": current_java,
            "target_version": target_java,
            "severity": "CRITICAL",
            "build_failure_risk": True,
            "source_changes_required": False,
            "action": f"Update Maven/Gradle compiler configuration to Java {target_java} and verify JDK API compatibility.",
        })

    # Preserve only genuinely high-severity/blocking issues from the full scan.
    for issue in report.get("issues", []):
        if issue.get("severity") not in {"HIGH", "BLOCKER"}:
            continue
        build_blockers.append({
            "component": issue.get("title"),
            "severity": issue.get("severity"),
            "path": issue.get("path"),
            "build_failure_risk": True,
            "action": issue.get("message"),
        })

    # API findings are more useful than a blanket list of outdated libraries.
    for category, findings in report.get("api_findings", {}).items():
        for finding in findings or []:
            if finding.get("severity") not in {"BLOCKER", "HIGH"}:
                continue
            api_findings.append({
                "category": category,
                "rule_id": finding.get("ruleId"),
                "severity": finding.get("severity"),
                "message": finding.get("message") or finding.get("description"),
                "path": finding.get("path") or finding.get("file"),
                "action": finding.get("action") or "Update the affected API usage and re-run the audit.",
            })

    # Only dependencies with known migration significance are retained.
    for dep in report.get("dependencies", []):
        name = dep.get("name", "")
        if name not in CRITICAL_COORDINATES:
            continue
        current = dep.get("version")
        recommended = dep.get("recommended_version")
        if not current or current == "Not resolved":
            continue
        critical_dependencies.append({
            "name": name,
            "current_version": current,
            "recommended_version": recommended,
            "severity": "CRITICAL" if dep.get("version_status") in {"MAJOR_UPDATE_AVAILABLE", "NEEDS_CHANGES"} else "HIGH",
            "build_failure_risk": dep.get("version_status") in {"MAJOR_UPDATE_AVAILABLE", "NEEDS_CHANGES"},
            "source_changes_required": name == "io.jsonwebtoken:jjwt" or "javax." in name,
            "action": _dependency_action(dep),
        })

    # Frameworks are included only when their version is actually outdated or
    # requires changes; routine patch updates are intentionally omitted.
    for framework in report.get("frameworks", []):
        if framework.get("name") not in CRITICAL_FRAMEWORKS:
            continue
        status = framework.get("version_status")
        if status not in {"MAJOR_UPDATE_AVAILABLE", "NEEDS_CHANGES"}:
            continue
        framework_migrations.append({
            "name": framework.get("name"),
            "current_version": framework.get("version"),
            "recommended_version": framework.get("recommended_version"),
            "severity": "CRITICAL",
            "build_failure_risk": True,
            "source_changes_required": True,
            "action": "Align this framework with the selected Spring/Java generation; inspect API and configuration changes before upgrading.",
        })

    # Deduplicate identical objects while preserving order.
    def unique(items):
        seen = set()
        result = []
        for item in items:
            key = repr(sorted(item.items()))
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    build_blockers = unique(build_blockers)
    migration_required = unique(migration_required)
    critical_dependencies = unique(critical_dependencies)
    framework_migrations = unique(framework_migrations)
    api_findings = unique(api_findings)

    return {
        "report_type": "critical_migration_only",
        "auditor_version": report.get("auditor_version"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_name": report.get("project_name"),
        "build_system": report.get("build_system"),
        "current_java": current_java,
        "target_java": target_java,
        "summary": {
            "build_blockers": len(build_blockers),
            "critical_dependencies": len(critical_dependencies),
            "framework_migrations": len(framework_migrations),
            "api_findings": len(api_findings),
            "routine_dependencies_excluded": max(
                0,
                report.get("scan_summary", {}).get("dependencies_detected", 0)
                - len(critical_dependencies),
            ),
        },
        "build_blockers": build_blockers,
        "critical_dependencies": critical_dependencies,
        "framework_migrations": framework_migrations,
        "api_findings": api_findings,
        "migration_required": migration_required,
        "note": "This report intentionally excludes routine dependency updates and transitive dependencies unless they are associated with a known build or migration risk. The full output.json remains the detailed audit.",
    }
