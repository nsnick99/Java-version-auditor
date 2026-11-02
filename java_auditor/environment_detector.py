"""Detect non-Java and third-party project components without affecting Java analysis."""

from __future__ import annotations

import re
from pathlib import Path

from .utils import iter_project_files, rel


FILE_TYPES = {
    "package.json": ("NODE_PROJECT", "SUPPORTING"),
    "package-lock.json": ("NODE_PROJECT", "SUPPORTING"),
    "yarn.lock": ("NODE_PROJECT", "SUPPORTING"),
    "pnpm-lock.yaml": ("NODE_PROJECT", "SUPPORTING"),
    "requirements.txt": ("PYTHON_PROJECT", "SUPPORTING"),
    "pyproject.toml": ("PYTHON_PROJECT", "SUPPORTING"),
    "Pipfile": ("PYTHON_PROJECT", "SUPPORTING"),
    "Dockerfile": ("DOCKER", "SUPPORTING"),
    "docker-compose.yml": ("DOCKER", "SUPPORTING"),
    "docker-compose.yaml": ("DOCKER", "SUPPORTING"),
    "Jenkinsfile": ("CI_CD", "SUPPORTING"),
    ".gitlab-ci.yml": ("CI_CD", "SUPPORTING"),
    "azure-pipelines.yml": ("CI_CD", "SUPPORTING"),
    "bitbucket-pipelines.yml": ("CI_CD", "SUPPORTING"),
}

EXTENSION_TYPES = {
    ".sql": ("DATABASE_SQL", "SUPPORTING"),
    ".tf": ("TERRAFORM", "INFRASTRUCTURE"),
    ".jsp": ("JSP", "JAVA_WEB"),
    ".wsdl": ("SOAP_WSDL", "EXTERNAL_INTEGRATION"),
    ".graphql": ("GRAPHQL", "EXTERNAL_INTEGRATION"),
    ".bat": ("WINDOWS_SCRIPT", "BUILD_OR_DEPLOYMENT"),
    ".cmd": ("WINDOWS_SCRIPT", "BUILD_OR_DEPLOYMENT"),
    ".sh": ("SHELL_SCRIPT", "BUILD_OR_DEPLOYMENT"),
    ".ps1": ("POWERSHELL_SCRIPT", "BUILD_OR_DEPLOYMENT"),
}

API_PATTERNS = [
    (re.compile(r"https?://[^\s\"'<>]+", re.I), "REST_ENDPOINT", "EXTERNAL_INTEGRATION"),
    (re.compile(r"(?:RestTemplate|WebClient|HttpClient|URLConnection)\b"), "HTTP_CLIENT", "EXTERNAL_INTEGRATION"),
    (re.compile(r"(?:FeignClient|OpenFeign)\b"), "FEIGN_CLIENT", "EXTERNAL_INTEGRATION"),
    (re.compile(r"(?:@WebService|@WebMethod|SOAPMessage|JaxWs)\b"), "SOAP_CLIENT", "EXTERNAL_INTEGRATION"),
]

SDK_MARKERS = {
    "aws": "CLOUD_AWS",
    "azure": "CLOUD_AZURE",
    "google-cloud": "CLOUD_GCP",
    "googleapis": "GOOGLE_API",
    "stripe": "STRIPE_SDK",
    "twilio": "TWILIO_SDK",
    "kafka": "KAFKA",
    "rabbitmq": "RABBITMQ",
}


def detect_environment(root: Path) -> dict:
    root = Path(root).resolve()
    components = []
    categories = set()
    environments = {"java": False, "maven": False, "gradle": False}
    external_integrations = []
    seen_components = set()
    seen_integrations = set()

    for path in iter_project_files(root):
        name = path.name
        lower_name = name.lower()
        suffix = path.suffix.lower()

        if suffix == ".java":
            environments["java"] = True

        if name == "pom.xml":
            environments["maven"] = True
        elif lower_name in {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}:
            environments["gradle"] = True

        item = FILE_TYPES.get(name) or FILE_TYPES.get(lower_name)
        if not item and suffix in EXTENSION_TYPES:
            item = EXTENSION_TYPES[suffix]
        if item:
            kind, impact = item
            key = (kind, rel(path, root))
            if key not in seen_components:
                seen_components.add(key)
                components.append({"type": kind, "path": rel(path, root), "impact": impact})
                categories.add(kind)

        if suffix in {".java", ".xml", ".yml", ".yaml", ".properties", ".json", ".gradle", ".kts"}:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sample = text[:500_000]
            for pattern, kind, impact in API_PATTERNS:
                if pattern.search(sample):
                    key = (kind, rel(path, root))
                    if key not in seen_integrations:
                        seen_integrations.add(key)
                        external_integrations.append({"type": kind, "path": rel(path, root), "impact": impact})
            lowered = sample.lower()
            for marker, kind in SDK_MARKERS.items():
                if marker in lowered:
                    key = (kind, rel(path, root))
                    if key not in seen_integrations:
                        seen_integrations.add(key)
                        external_integrations.append({"type": kind, "path": rel(path, root), "impact": "EXTERNAL_DEPENDENCY"})

    environments.update({
        "nodejs": "NODE_PROJECT" in categories,
        "python": "PYTHON_PROJECT" in categories,
        "docker": "DOCKER" in categories,
        "ci_cd": "CI_CD" in categories,
        "database": "DATABASE_SQL" in categories,
        "infrastructure": "TERRAFORM" in categories,
    })
    components.sort(key=lambda x: (x["type"], x["path"]))
    external_integrations.sort(key=lambda x: (x["type"], x["path"]))

    return {
        "detector_version": "1.0",
        "environment": environments,
        "non_java_components": components,
        "external_integrations": external_integrations,
        "summary": {
            "non_java_component_count": len(components),
            "external_integration_count": len(external_integrations),
        },
    }
