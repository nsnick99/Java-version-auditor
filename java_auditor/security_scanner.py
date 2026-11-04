"""Conservative source/config secret detection for project directories."""
from __future__ import annotations
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from .utils import iter_project_files, rel

_ALLOWED = {".properties", ".yml", ".yaml", ".json", ".xml", ".env", ".ini", ".conf", ".cfg", ".toml"}
_PATTERNS = [
    ("HARDCODED_PASSWORD", re.compile(r"""(?i)\b(?:password|passwd|pwd)\b\s*[:=]\s*['"]?([^\s'"#},;]+)""")),
    ("HARDCODED_SECRET", re.compile(r"""(?i)\b(?:secret|client_secret|api_secret|access_token|auth_token)\b\s*[:=]\s*['"]?([^\s'"#},;]+)""")),
    ("HARDCODED_API_KEY", re.compile(r"""(?i)\b(?:api[_-]?key|access[_-]?key)\b\s*[:=]\s*['"]?([^\s'"#},;]+)""")),
]
_PLACEHOLDERS = {"", "null", "none", "changeme", "change_me", "your_password", "your_secret", "your_api_key"}

def scan_secrets(root: Path) -> list[dict]:
    """Find likely literal credentials; never copy secret values into reports."""
    findings = []
    for path in iter_project_files(root):
        if path.suffix.lower() not in _ALLOWED and path.name not in {".env", ".env.local"}:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//", "<!--")):
                continue
            for kind, pattern in _PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                value = match.group(1).strip().strip("'\"")
                if value.lower() in _PLACEHOLDERS or value.startswith(("$", "${")) or len(value) < 4:
                    continue
                findings.append({
                    "id": kind, "severity": "HIGH", "path": rel(path, root),
                    "line": line_no, "evidence": f"{kind} assignment detected; value redacted",
                    "recommendation": "Move the credential to a secret manager or environment variable; rotate it if committed.",
                })
                break
    return findings


def scan_dependency_vulnerabilities(dependencies, timeout: int = 5) -> dict:
    """Query OSV in one batch. Returns explicit status when the service is unavailable."""
    packages = []
    for dep in dependencies:
        group, artifact, version = dep.get("group_id"), dep.get("artifact_id"), dep.get("version")
        if not group or not artifact or not version or version == "Not resolved":
            continue
        packages.append({
            "name": f"{group}:{artifact}",
            "version": str(version),
            "query": {"package": {"purl": f"pkg:maven/{group}/{artifact}"}, "version": str(version)}
        })
    if not packages:
        return {"status": "NO_RESOLVED_DEPENDENCIES", "findings": []}
    payload = json.dumps({"queries": [p["query"] for p in packages]}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.osv.dev/v1/querybatch", data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Java-Version-Auditor"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"status": "UNAVAILABLE", "message": str(exc)[:180], "findings": []}
    findings = []
    for package, result in zip(packages, data.get("results", [])):
        for vuln in result.get("vulns", []) or []:
            severity = "UNKNOWN"
            cvss_score = None
            for item in vuln.get("severity", []) or []:
                score = item.get("score", "")
                match = re.search(r"(?:CVSS:[^/]+/)?(\d+(?:\.\d+)?)$", str(score))
                if match:
                    cvss_score = float(match.group(1))
                    severity = "CRITICAL" if cvss_score >= 9 else "HIGH" if cvss_score >= 7 else "MEDIUM" if cvss_score >= 4 else "LOW"
                    break
            findings.append({
                "id": vuln.get("id"),
                "aliases": vuln.get("aliases", []),
                "package": package["name"],
                "version": package["version"],
                "severity": severity,
                "cvss_score": cvss_score,
                "summary": vuln.get("summary") or vuln.get("details", "")[:300],
                "modified": vuln.get("modified"),
                "source": "OSV",
            })
    return {"status": "COMPLETED", "findings": findings}
