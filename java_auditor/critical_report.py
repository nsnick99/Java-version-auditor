"""Evidence-based summary of critical technical findings."""
from __future__ import annotations
from datetime import datetime

def build_critical_report(report):
    findings = []
    for issue in report.get("issues", []):
        sev = str(issue.get("severity", "")).upper()
        if sev in {"CRITICAL", "BLOCKER", "HIGH"} and "credential" not in str(issue.get("title", "")).lower():
            findings.append({
                "category": "security" if any(k in str(issue.get("title", "")).lower() for k in ("credential", "vulnerability")) else "technical",
                "title": issue.get("title", "Technical issue"), "severity": sev,
                "path": issue.get("path"), "message": issue.get("message"),
                "evidence": issue.get("evidence")
            })
    for group, items in (report.get("api_findings") or {}).items():
        if group == "namespaceMigration":
            continue
        for item in items or []:
            sev = str(item.get("severity", "")).upper()
            if sev not in {"CRITICAL", "BLOCKER", "HIGH"}:
                continue
            findings.append({
                "category": group, "title": item.get("ruleId", "API finding"),
                "severity": sev, "path": item.get("path") or item.get("file"),
                "locations": item.get("locations", []),
                "message": item.get("message") or item.get("description") or item.get("replacement"),
                "recommendation": item.get("replacement") or item.get("suggestedAlternative")
            })
    for item in report.get("security_findings", []):
        sev = str(item.get("severity", "")).upper()
        if sev in {"CRITICAL", "BLOCKER", "HIGH"}:
            findings.append({
                "category": "security", "title": item.get("id", "Security finding"),
                "severity": sev, "path": item.get("path"), "line": item.get("line"),
                "evidence": item.get("evidence"), "recommendation": item.get("recommendation")
            })
    unique = {}
    for item in findings:
        key = (item.get("category"), item.get("title"), item.get("path"), item.get("line"), repr(item.get("locations")))
        unique[key] = item
    findings = sorted(unique.values(), key=lambda x: ({"CRITICAL":0,"BLOCKER":1,"HIGH":2}.get(x.get("severity"),3), str(x.get("path") or ""), str(x.get("title") or "")))
    compatibility = []
    for module in report.get("modules", []):
        current, target = str(module.get("java_version", "")), str(report.get("target_java", ""))
        if current.isdigit() and target.isdigit() and int(current) < int(target):
            compatibility.append({
                "category": "target_java_baseline", "severity": "INFO",
                "module": module.get("path"), "current_java": current,
                "requested_target_java": target,
                "message": "Older than the requested comparison target; this alone does not mean the current build is broken."
            })
    return {
        "report_type": "critical_technical_findings",
        "auditor_version": report.get("auditor_version"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_name": report.get("project_name"), "build_system": report.get("build_system"),
        "current_java": report.get("current_java"),
        "summary": {
            "critical_findings": sum(x["severity"] == "CRITICAL" for x in findings),
            "blocker_findings": sum(x["severity"] == "BLOCKER" for x in findings),
            "high_findings": sum(x["severity"] == "HIGH" for x in findings),
            "compatibility_notes": len(compatibility)
        },
        "findings": findings, "compatibility_notes": compatibility,
        "note": "Only evidenced technical risks are included. A requested target Java version or javax-to-jakarta difference is not a current build blocker by itself. An available dependency update is not proof of a vulnerability."
    }
