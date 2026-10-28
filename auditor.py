from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# --- CONFIGURATION ---
USER_AGENT = "Java-Version-Auditor/1.0"
HTTP_TIMEOUT = 8

# Known advisories (simplified)
KNOWN_ADVISORIES = [
    {
        "matches": [{"group": "com.netflix.hystrix"}],
        "status": "MAINTENANCE_MODE",
        "name": "Netflix Hystrix",
        "reason": "No longer in active development. Use Resilience4j or Spring Cloud CircuitBreaker.",
        "alternative": "Resilience4j / Spring Cloud CircuitBreaker",
        "source_url": "https://github.com/Netflix/Hystrix",
    },
    {
        "matches": [
            {"group": "com.netflix.ribbon"},
            {"group": "org.springframework.cloud", "artifact_prefix": "spring-cloud-starter-netflix-ribbon"},
        ],
        "status": "MAINTENANCE_MODE",
        "name": "Netflix Ribbon",
        "reason": "Deprecated by Spring Cloud. Use Spring Cloud LoadBalancer instead.",
        "alternative": "Spring Cloud LoadBalancer",
        "source_url": "https://docs.spring.io/spring-cloud-netflix/docs/2.2.8.RELEASE/reference/html/",
    },
]


# --- UTILITIES ---
def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: str | None) -> str | None:
    if not value:
        return None
    value = str(value).strip().strip("'\"")
    if not value or value.startswith("${"):
        return None
    match = re.fullmatch(r"(?:JavaVersion\.)?VERSION_(?:1_)?(\d+)", value, re.I)
    return match.group(1) if match else value


def java_major(value: str | None) -> str | None:
    value = clean(value)
    if not value:
        return None
    if value.startswith("1.") and len(value.split(".")) > 1:
        return value.split(".")[1]
    match = re.match(r"\d+", value)
    return match.group(0) if match else value


def version_numbers(value: str | None) -> list[int]:
    if not value:
        return []
    match = re.match(r"^\s*[vV]?(\d+(?:\.\d+)*)", str(value))
    if not match:
        return []
    try:
        return [int(part) for part in match.group(1).split(".")]
    except ValueError:
        return []


def compare_versions(left: str, right: str) -> int:
    left_nums = version_numbers(left)
    right_nums = version_numbers(right)
    if not left_nums or not right_nums:
        return 0
    width = max(len(left_nums), len(right_nums))
    left_nums += [0] * (width - len(left_nums))
    right_nums += [0] * (width - len(right_nums))
    if left_nums < right_nums:
        return -1
    if left_nums > right_nums:
        return 1
    # Prerelease check
    left_tail = str(left).strip().lower()[len(re.match(r"^\s*[vV]?\d+(?:\.\d+)*", str(left)).group(0).strip()):]
    right_tail = str(right).strip().lower()[len(re.match(r"^\s*[vV]?\d+(?:\.\d+)*", str(right)).group(0).strip()):]
    prerelease = re.compile(r"(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea|cr)\b", re.I)
    left_pre = bool(prerelease.search(left_tail))
    right_pre = bool(prerelease.search(right_tail))
    if left_pre != right_pre:
        return -1 if left_pre else 1
    return 0


def http_json(url: str, timeout: int = HTTP_TIMEOUT) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def http_bytes(url: str, timeout: int = HTTP_TIMEOUT, accept: str = "*/*") -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def maven_metadata_url(group_id: str, artifact_id: str) -> str:
    group_path = "/".join(urllib.parse.quote(part, safe="") for part in str(group_id).split("."))
    artifact = urllib.parse.quote(str(artifact_id), safe="")
    return f"https://repo.maven.apache.org/maven2/{group_path}/{artifact}/maven-metadata.xml"


def latest_maven_version(group_id: str, artifact_id: str) -> dict:
    if not group_id or not artifact_id:
        return {"status": "NOT_APPLICABLE", "version": None}
    url = maven_metadata_url(group_id, artifact_id)
    try:
        raw = http_bytes(url, accept="application/xml,text/xml,*/*")
        root = ET.fromstring(raw)
    except urllib.error.HTTPError as e:
        return {"status": "NOT_FOUND" if e.code == 404 else "UNAVAILABLE", "version": None, "url": url}
    except Exception as e:
        return {"status": "UNAVAILABLE", "version": None, "url": url, "message": str(e)[:250]}

    def xml_text(name: str) -> str | None:
        node = next((item for item in list(root) if item.tag.split("}")[-1] == name), None)
        return (node.text or "").strip() if node is not None and node.text else None

    versioning = next((item for item in list(root) if item.tag.split("}")[-1] == "versioning"), None)
    versions_node = next((item for item in list(versioning or []) if item.tag.split("}")[-1] == "versions"), None)
    versions = [(item.text or "").strip() for item in list(versions_node or []) if item.text]
    stable = [v for v in versions if not re.search(r"(?i)(?:^|[._-])(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea|cr)", v)]
    release = xml_text("release")
    if release and not re.search(r"(?i)(?:^|[._-])(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea|cr)", release):
        chosen = release
    elif stable:
        chosen = stable[0]
        for v in stable[1:]:
            if compare_versions(v, chosen) > 0:
                chosen = v
    else:
        latest = xml_text("latest")
        chosen = latest if latest and not re.search(r"(?i)(?:^|[._-])(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea|cr)", latest) else None

    return {"status": "FOUND" if chosen else "NO_STABLE_RELEASE_FOUND", "version": chosen, "url": url}


def lifecycle_slug_for_component(group: str, artifact: str) -> str | None:
    if group == "org.springframework.boot" or artifact.startswith("spring-boot-"):
        return "spring-boot"
    if group == "org.springframework" and artifact.startswith("spring-"):
        return "spring-framework"
    if group == "org.springframework.security" or artifact.startswith("spring-security-"):
        return "spring-security"
    if group in {"org.hibernate", "org.hibernate.orm"} and artifact == "hibernate-core":
        return "hibernate"
    if group.startswith("org.apache.tomcat") or artifact.startswith("tomcat-"):
        return "tomcat"
    return None


def lifecycle_status(product: str, version: str) -> dict:
    slug = lifecycle_slug_for_component("", "")
    if product == "java":
        slug = "java"
    elif product == "spring-boot":
        slug = "spring-boot"
    else:
        return {"status": "NOT_CATALOGUED", "product": product, "version": version}

    url = f"https://endoflife.date/api/v1/products/{slug}"
    try:
        data = http_json(url)
        result = data.get("result", {})
        cycle = version.split(".")[:2] if "." in version else [version]
        cycle_str = ".".join(cycle)
        release = next((r for r in result.get("releases", []) if r.get("name") == cycle_str), None)
        if not release:
            return {"status": "CYCLE_NOT_FOUND", "product": product, "cycle": cycle_str, "version": version}
        eol = release.get("isEol")
        maintained = release.get("isMaintained")
        if eol:
            status = "EOL"
        elif maintained:
            status = "MAINTAINED"
        else:
            status = "NOT_MAINTAINED"
        return {
            "status": status,
            "product": product,
            "cycle": cycle_str,
            "version": version,
            "eol_date": release.get("eolFrom"),
            "is_eol": eol,
            "is_maintained": maintained,
            "source_url": result.get("links", {}).get("html", url),
        }
    except urllib.error.HTTPError as e:
        return {"status": "NOT_CATALOGUED" if e.code == 404 else "UNAVAILABLE", "product": product, "version": version}
    except Exception as e:
        return {"status": "UNAVAILABLE", "product": product, "version": version, "message": str(e)[:250]}


def known_deprecation(group: str, artifact: str) -> dict:
    for advisory in KNOWN_ADVISORIES:
        for rule in advisory["matches"]:
            if group == rule.get("group") or group.startswith(rule.get("group_prefix", "")):
                prefix = rule.get("artifact_prefix")
                exact = rule.get("artifact")
                if (not prefix and not exact) or (prefix and artifact.startswith(prefix)) or (exact and artifact == exact):
                    return {
                        "status": advisory["status"],
                        "name": advisory["name"],
                        "reason": advisory["reason"],
                        "recommended_alternative": advisory["alternative"],
                        "source_url": advisory["source_url"],
                    }
    return {"status": "NOT_VERIFIED"}


# --- NEW: PROJECT METADATA & FILE SUMMARY ---
def get_project_info(pom_path: Path, root: Path, java_version: str | None) -> dict:
    """Extract project metadata from POM and directory."""
    try:
        tree = ET.parse(pom_path)
        root_elem = tree.getroot()
    except Exception:
        return {}

    def child(name: str) -> ET.Element | None:
        return next((item for item in list(root_elem) if item.tag.split("}")[-1] == name), None)

    def text_child(name: str) -> str | None:
        elem = child(name)
        return (elem.text or "").strip() if elem is not None and elem.text else None

    # Extract POM fields
    name = text_child("name")
    if not name:
        name = root.name  # fallback to directory name

    artifact_id = text_child("artifactId") or root.name
    group_id = text_child("groupId")
    version = text_child("version")
    packaging = text_child("packaging") or "jar"

    # Infer target framework (Java version)
    target = java_version or text_child("maven.compiler.target") or text_child("maven.compiler.source")
    if target and target.startswith("1."):
        target = target.split(".")[1]  # e.g., "1.8" → "8"

    return {
        "name": name,
        "pom_file": str(pom_path.relative_to(root)),
        "project_type": "Maven",
        "target_framework": target or "unknown",
        "root_namespace": group_id or "unknown",
        "artifact_id": artifact_id,
        "version": version or "0.0.0-SNAPSHOT",
        "sdk": target,
        "output_type": "war" if packaging == "war" else "jar",
    }


def count_files_and_types(root: Path) -> dict:
    """Count files by extension in project (excluding .git, target, etc.)."""
    file_types = {}
    total = 0

    exclude_dirs = {".git", "target", "build", "out", "bin", "node_modules"}
    include_exts = {".java", ".xml", ".properties", ".yml", ".yaml", ".json", ".md", ".txt", ".sql", ".gradle", ".gradle.kts"}

    for path in root.rglob("*"):
        if path.is_file() and not any(excl in path.parts for excl in exclude_dirs):
            ext = path.suffix or "[no extension]"
            if ext in include_exts:
                file_types[ext] = file_types.get(ext, 0) + 1
                total += 1

    return {
        "totalFiles": total,
        "fileTypes": file_types,
    }


# --- PARSERS ---
def parse_pom(pom_path: Path, root: Path) -> dict:
    try:
        tree = ET.parse(pom_path)
        root_elem = tree.getroot()
    except Exception:
        return {"java": None, "boot": None, "dependencies": []}

    def child(name: str) -> ET.Element | None:
        return next((item for item in list(root_elem) if item.tag.split("}")[-1] == name), None)

    def text_child(name: str) -> str | None:
        elem = child(name)
        return (elem.text or "").strip() if elem is not None and elem.text else None

    # Java version
    java = None
    props = child("properties")
    if props is not None:
        for item in list(props):
            name = item.tag.split("}")[-1]
            val = (item.text or "").strip()
            if name in {"java.version", "maven.compiler.source", "maven.compiler.target"}:
                java = java_major(val)
                break

    # Spring Boot version
    boot = None
    parent = child("parent")
    if parent is not None:
        pg = text_child("groupId")
        pa = text_child("artifactId")
        pv = text_child("version")
        if pg == "org.springframework.boot" and pa == "spring-boot-starter-parent":
            boot = clean(pv)

    # Dependencies
    deps = []
    deps_node = child("dependencies")
    if deps_node is not None:
        for dep in list(deps_node):
            group = text_child("groupId")
            artifact = text_child("artifactId")
            version = text_child("version")
            if group and artifact:
                deps.append({
                    "group_id": group,
                    "artifact_id": artifact,
                    "version": clean(version),
                    "scope": (text_child("scope") or "compile"),
                    "optional": (text_child("optional") or "").lower() == "true",
                })

    return {"java": java, "boot": boot, "dependencies": deps}


def audit(project: str, resolve: bool = False) -> dict:
    root = Path(project).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("Project path must be an existing directory")

    # Find build files
    build_files = []
    for path in root.rglob("*"):
        if path.is_file() and path.name in {"pom.xml", "build.gradle", "build.gradle.kts"}:
            if not any(part in {".git", "target", "build", "out", "bin", "node_modules"} for part in path.parts):
                build_files.append(path)

    if not build_files:
        return {"project": root.name, "summary": "No supported build files found"}

    # Parse first build file (simplified: assume single module)
    pom_path = next((f for f in build_files if f.name == "pom.xml"), None)
    if pom_path:
        data = parse_pom(pom_path, root)
        # Add project info and file summary
        project_info = get_project_info(pom_path, root, data["java"])
        file_summary = count_files_and_types(root)
    else:
        return {"project": root.name, "summary": "Only Maven projects supported in this minimal version"}

    # Enrich with version intelligence
    updates = []
    for dep in data["dependencies"]:
        group = dep["group_id"]
        artifact = dep["artifact_id"]
        version = dep["version"]

        # Skip if no version
        if not version:
            continue

        # Get latest version
        latest = latest_maven_version(group, artifact)
        latest_version = latest.get("version")

        # Check for outdated
        if latest["status"] == "FOUND" and latest_version:
            if compare_versions(version, latest_version) < 0:
                updates.append({
                    "module": "backend",
                    "name": artifact,
                    "current_version": version,
                    "latest_version": latest_version,
                    "component": f"{group}:{artifact}",
                    "reason": "Newer stable release available",
                    "explanation": f"The current version ({version}) is older than the latest stable release ({latest_version}). Updating ensures access to security patches, bug fixes, and new features.",
                    "status": "OUTDATED",
                    "action": f"Update to {latest_version} and verify compatibility",
                    "source_url": latest["url"],
                })

        # Check lifecycle
        if group == "org.springframework.boot" or artifact.startswith("spring-boot-"):
            lifecycle = lifecycle_status("spring-boot", version)
            if lifecycle["status"] == "EOL":
                updates.append({
                    "module": "backend",
                    "name": artifact,
                    "current_version": version,
                    "latest_version": "3.3.x",
                    "component": f"{group}:{artifact}",
                    "reason": "End-of-life version",
                    "explanation": f"Spring Boot {version} has reached end-of-life and no longer receives security updates or bug fixes. Continuing to use it exposes your application to known vulnerabilities.",
                    "status": "EOL",
                    "action": "Upgrade to a maintained Spring Boot version (e.g., 3.3.x)",
                    "source_url": lifecycle["source_url"],
                })

        # Check deprecation
        deprecation = known_deprecation(group, artifact)
        if deprecation["status"] == "MAINTENANCE_MODE":
            updates.append({
                "module": "backend",
                "name": artifact,
                "current_version": version,
                "latest_version": deprecation["recommended_alternative"],
                "component": f"{group}:{artifact}",
                "reason": deprecation["reason"],
                "explanation": f"{deprecation['name']} is no longer actively developed and only receives critical fixes. Continuing to use it may lead to compatibility issues with newer libraries and frameworks.",
                "status": "MAINTENANCE_MODE",
                "action": deprecation["recommended_alternative"],
                "source_url": deprecation["source_url"],
            })

    # Technology checks
    tech_updates = []
    if data["java"]:
        lifecycle = lifecycle_status("java", data["java"])
        if lifecycle["status"] == "EOL":
            tech_updates.append({
                "module": "backend",
                "name": "Java",
                "current_version": data["java"],
                "latest_version": "21",
                "component": "Java",
                "reason": "End-of-life version",
                "explanation": f"Java {data['java']} has reached end-of-life and no longer receives security patches. This affects all applications running on this JDK version.",
                "status": "EOL",
                "action": "Upgrade to Java 17 or 21 (LTS)",
                "source_url": lifecycle["source_url"],
            })

    if data["boot"]:
        lifecycle = lifecycle_status("spring-boot", data["boot"])
        if lifecycle["status"] == "EOL":
            tech_updates.append({
                "module": "backend",
                "name": "Spring Boot",
                "current_version": data["boot"],
                "latest_version": "3.3.x",
                "component": "Spring Boot",
                "reason": "End-of-life version",
                "explanation": f"Spring Boot {data['boot']} has reached end-of-life and no longer receives security updates. This affects all Spring Boot applications in your project.",
                "status": "EOL",
                "action": "Upgrade to Spring Boot 3.3.x",
                "source_url": lifecycle["source_url"],
            })

    return {
        "project": project_info,  # ← NEW: full project metadata
        "path": str(root),
        "timestamp": now_utc(),
        "fileSummary": file_summary,  # ← NEW: file counts and types
        "summary": {
            "total_dependencies": len(data["dependencies"]),
            "outdated_components": len(updates),
            "outdated_technologies": len(tech_updates),
            "status": "ALL_CURRENT" if not updates and not tech_updates else "UPDATES_REQUIRED",
        },
        "outdated_components": updates,
        "outdated_technologies": tech_updates,
    }


def main():
    parser = argparse.ArgumentParser(description="Audit Java project for outdated versions")
    parser.add_argument("project", help="Path to Java project")
    parser.add_argument("-o", "--output", default="audit-report.json", help="Output JSON file")
    parser.add_argument("--resolve", action="store_true", help="Run Maven/Gradle to resolve dependencies")
    args = parser.parse_args()

    try:
        report = audit(args.project, resolve=args.resolve)
    except ValueError as e:
        parser.error(str(e))

    # Write report
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Print summary
    print(f"Project: {report['project']['name']}")
    print(f"Status: {report['summary']['status']}")
    print(f"Outdated components: {report['summary']['outdated_components']}")
    print(f"Outdated technologies: {report['summary']['outdated_technologies']}")
    print(f"Total files analyzed: {report['fileSummary']['totalFiles']}")

    if report["outdated_components"]:
        print("\nComponents needing update:")
        for item in report["outdated_components"]:
            print(f"- {item['component']}")
            print(f"  Module: {item['module']}")
            print(f"  Name: {item['name']}")
            print(f"  Current Version: {item['current_version']}")
            print(f"  Latest Version: {item['latest_version']}")
            print(f"  Reason: {item['reason']}")
            print(f"  Explanation: {item['explanation']}")
            print(f"  Status: {item['status']}")
            print(f"  Action: {item['action']}")
            print(f"  Source URL: {item['source_url']}")
            print()

    if report["outdated_technologies"]:
        print("\nTechnologies needing update:")
        for item in report["outdated_technologies"]:
            print(f"- {item['component']}")
            print(f"  Module: {item['module']}")
            print(f"  Name: {item['name']}")
            print(f"  Current Version: {item['current_version']}")
            print(f"  Latest Version: {item['latest_version']}")
            print(f"  Reason: {item['reason']}")
            print(f"  Explanation: {item['explanation']}")
            print(f"  Status: {item['status']}")
            print(f"  Action: {item['action']}")
            print(f"  Source URL: {item['source_url']}")
            print()

    print(f"Report saved to: {output}")


if __name__ == "__main__":
    main()
