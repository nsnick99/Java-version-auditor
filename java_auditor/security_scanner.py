"""Conservative source/config secret detection for project directories."""
from __future__ import annotations
import re
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
