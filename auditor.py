from __future__ import annotations

import argparse
import ipaddress
import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
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


def xml_child(parent: ET.Element | None, name: str) -> ET.Element | None:
    if parent is None:
        return None
    return next((item for item in list(parent) if item.tag.split("}")[-1] == name), None)


def xml_text(parent: ET.Element | None, name: str) -> str | None:
    node = xml_child(parent, name)
    return (node.text or "").strip() if node is not None and node.text else None


def is_internal_repository_url(url: str, repository_id: str = "") -> bool:
    """Accept private/local endpoints only in strict internal-only mode."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            return False
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".local", ".internal")):
            return True
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            repo_id = repository_id.lower()
            return "." not in host and any(
                token in repo_id for token in ("internal", "intranet", "nexus", "artifactory")
            )
    except ValueError:
        return False


def configured_maven_repositories(pom_path: Path, internal_only: bool = False) -> list[dict]:
    """Read repository declarations from the project POM; edit.txt is not consulted."""
    try:
        root = ET.parse(pom_path).getroot()
    except (ET.ParseError, OSError):
        return []
    repositories = []
    for container_name, repository_name in (
        ("repositories", "repository"),
        ("pluginRepositories", "pluginRepository"),
    ):
        container = xml_child(root, container_name)
        for repository in list(container or []):
            if repository.tag.split("}")[-1] != repository_name:
                continue
            repository_id = xml_text(repository, "id") or ""
            url = xml_text(repository, "url")
            if not url:
                continue
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                continue
            if internal_only and not is_internal_repository_url(url, repository_id):
                continue
            normalized = url.rstrip("/")
            if not any(item["url"] == normalized for item in repositories):
                repositories.append({"id": repository_id or "repository", "url": normalized})
    return repositories


def maven_metadata_url(group_id: str, artifact_id: str, repository_url: str | None = None) -> str:
    group_path = "/".join(urllib.parse.quote(part, safe="") for part in str(group_id).split("."))
    artifact = urllib.parse.quote(str(artifact_id), safe="")
    base = repository_url.rstrip("/") if repository_url else "https://repo.maven.apache.org/maven2"
    return f"{base}/{group_path}/{artifact}/maven-metadata.xml"


def latest_maven_version(
    group_id: str,
    artifact_id: str,
    repository_urls: list[dict] | None = None,
    internal_only: bool = False,
    offline: bool = False,
) -> dict:
    """Look up versions; internal-only never falls back to a public URL."""
    if not group_id or not artifact_id:
        return {"status": "NOT_APPLICABLE", "version": None}
    if offline:
        return {"status": "SKIPPED_OFFLINE", "version": None}
    if internal_only:
        candidates = [
            repo for repo in (repository_urls or [])
            if is_internal_repository_url(repo.get("url", ""), repo.get("id", ""))
        ]
        if not candidates:
            return {
                "status": "SKIPPED_NO_INTERNAL_REPOSITORY",
                "version": None,
                "message": "No approved internal Maven repository URL was found in this POM.",
            }
    else:
        candidates = [{"id": "maven-central", "url": "https://repo.maven.apache.org/maven2"}]

    last_failure = None
    for repository in candidates:
        url = maven_metadata_url(group_id, artifact_id, repository["url"])
        try:
            raw = http_bytes(url, accept="application/xml,text/xml,*/*")
            root = ET.fromstring(raw)
        except urllib.error.HTTPError as error:
            if internal_only:
                last_failure = {
                    "status": "NOT_FOUND" if error.code == 404 else "UNAVAILABLE",
                    "version": None,
                    "message": "Internal repository returned HTTP " + str(error.code),
                }
            else:
                last_failure = {
                    "status": "NOT_FOUND" if error.code == 404 else "UNAVAILABLE",
                    "version": None, "url": url, "message": "HTTP " + str(error.code),
                }
            continue
        except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError, ValueError):
            last_failure = {
                "status": "UNAVAILABLE", "version": None,
                "url": None if internal_only else url,
                "message": "Internal repository lookup failed." if internal_only else "Repository metadata could not be retrieved.",
            }
            continue

        versioning = xml_child(root, "versioning")
        versions_node = xml_child(versioning, "versions")
        versions = [
            (item.text or "").strip() for item in list(versions_node or [])
            if item.tag.split("}")[-1] == "version" and item.text
        ]
        prerelease = re.compile(
            r"(?i)(?:^|[._-])(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea|cr)(?:[._-]?\d+)?(?:$|[._-])"
        )
        stable = [version for version in versions if not prerelease.search(version)]
        release = xml_text(versioning, "release")
        if release and not prerelease.search(release):
            chosen = release
        elif stable:
            chosen = stable[0]
            for version in stable[1:]:
                if compare_versions(version, chosen) > 0:
                    chosen = version
        else:
            latest = xml_text(versioning, "latest")
            chosen = latest if latest and not prerelease.search(latest) else None
        if not chosen:
            last_failure = {
                "status": "NO_STABLE_RELEASE_FOUND", "version": None,
                "message": "No stable version was present in repository metadata.",
            }
            continue
        return {
            "status": "FOUND", "version": chosen,
            "url": None if internal_only else url,
            "source": "internal Maven repository" if internal_only else "Maven Central",
        }
    return last_failure or {"status": "NOT_FOUND", "version": None}




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


def lifecycle_status(product: str, version: str, allow_external: bool = True) -> dict:
    """Query public lifecycle data only when external lookups are explicitly allowed."""
    if not allow_external:
        return {
            "status": "SKIPPED", "product": product, "version": version,
            "message": "Lifecycle data is not available from Maven repository metadata.",
        }
    slug = "java" if product == "java" else "spring-boot" if product == "spring-boot" else None
    if slug is None:
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
        eol, maintained = release.get("isEol"), release.get("isMaintained")
        status = "EOL" if eol else "MAINTAINED" if maintained else "NOT_MAINTAINED"
        return {
            "status": status, "product": product, "cycle": cycle_str, "version": version,
            "eol_date": release.get("eolFrom"), "is_eol": eol, "is_maintained": maintained,
            "source_url": result.get("links", {}).get("html", url),
        }
    except urllib.error.HTTPError as error:
        return {"status": "NOT_CATALOGUED" if error.code == 404 else "UNAVAILABLE", "product": product, "version": version}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {"status": "UNAVAILABLE", "product": product, "version": version, "message": "Lifecycle source could not be reached."}




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
    """Parse direct Maven declarations with XML lookups scoped to each node."""
    try:
        root_elem = ET.parse(pom_path).getroot()
    except (ET.ParseError, OSError):
        return {"java": None, "boot": None, "dependencies": [], "repositories": []}

    properties_node = xml_child(root_elem, "properties")
    properties = {
        item.tag.split("}")[-1]: (item.text or "").strip()
        for item in list(properties_node or [])
        if item.text
    }

    def resolve_property(value: str | None) -> str | None:
        if not value:
            return None
        result = value.strip()
        for _ in range(8):
            changed = False
            for key, replacement in properties.items():
                token = chr(36) + "{" + key + "}"
                if token in result and replacement:
                    result = result.replace(token, replacement)
                    changed = True
            if not changed:
                break
        return clean(result)

    java = None
    for property_name in ("java.version", "maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "jdk.version"):
        value = resolve_property(properties.get(property_name))
        if value:
            java = java_major(value)
            break
    if java is None:
        build = xml_child(root_elem, "build")
        plugins = xml_child(build, "plugins")
        for plugin in list(plugins or []):
            if xml_text(plugin, "artifactId") != "maven-compiler-plugin":
                continue
            configuration = xml_child(plugin, "configuration")
            value = resolve_property(
                xml_text(configuration, "release")
                or xml_text(configuration, "source")
                or xml_text(configuration, "target")
            )
            java = java_major(value)
            if java:
                break

    parent = xml_child(root_elem, "parent")
    parent_group = xml_text(parent, "groupId")
    parent_artifact = xml_text(parent, "artifactId")
    parent_version = resolve_property(xml_text(parent, "version"))
    boot = parent_version if (
        parent_group == "org.springframework.boot" and parent_artifact == "spring-boot-starter-parent"
    ) else None

    managed_versions = {}
    management = xml_child(xml_child(root_elem, "dependencyManagement"), "dependencies")
    for managed in list(management or []):
        if managed.tag.split("}")[-1] != "dependency":
            continue
        group = resolve_property(xml_text(managed, "groupId"))
        artifact = resolve_property(xml_text(managed, "artifactId"))
        version = resolve_property(xml_text(managed, "version"))
        if group and artifact and version:
            managed_versions[(group, artifact)] = version

    deps = []
    deps_node = xml_child(root_elem, "dependencies")
    for dep in list(deps_node or []):
        if dep.tag.split("}")[-1] != "dependency":
            continue
        group = resolve_property(xml_text(dep, "groupId"))
        artifact = resolve_property(xml_text(dep, "artifactId"))
        version = resolve_property(xml_text(dep, "version"))
        if not version and group and artifact:
            version = managed_versions.get((group, artifact))
        if group and artifact:
            deps.append({
                "group_id": group,
                "artifact_id": artifact,
                "version": version,
                "scope": resolve_property(xml_text(dep, "scope")) or "compile",
                "optional": (resolve_property(xml_text(dep, "optional")) or "").lower() == "true",
            })

    return {
        "java": java,
        "boot": boot,
        "dependencies": deps,
        "repositories": configured_maven_repositories(pom_path),
    }




def resolve_effective_pom(
    pom_path: Path,
    repositories: list[dict],
    internal_only: bool = False,
    offline: bool = False,
) -> tuple[dict | None, str | None]:
    """Resolve parent/BOM-managed versions; internal-only mirrors Maven to one approved repo."""
    if shutil.which("mvn") is None:
        return None, "Maven executable was not found; inherited dependency versions remain unresolved."

    internal_repo = None
    if internal_only:
        internal_repo = next(
            (repo for repo in repositories if is_internal_repository_url(repo.get("url", ""), repo.get("id", ""))),
            None,
        )
        if internal_repo is None:
            return None, "No approved internal Maven repository is configured in this POM."

    user_settings = Path.home() / ".m2" / "settings.xml"
    try:
        settings_root = ET.parse(user_settings).getroot() if user_settings.is_file() else ET.Element("settings")
    except (ET.ParseError, OSError):
        if internal_only:
            return None, "Maven settings.xml could not be parsed; resolution stopped to avoid public fallback."
        settings_root = ET.Element("settings")

    namespace = settings_root.tag[1:].split("}")[0] if settings_root.tag.startswith("{") else ""
    def q(name: str) -> str:
        return "{" + namespace + "}" + name if namespace else name

    temp = tempfile.TemporaryDirectory(prefix="java-version-auditor-")
    temp_dir = Path(temp.name)
    settings_path = None
    effective_path = temp_dir / "effective-pom.xml"
    if internal_only:
        if namespace:
            ET.register_namespace("", namespace)
        mirrors = settings_root.find(q("mirrors"))
        if mirrors is None:
            mirrors = ET.SubElement(settings_root, q("mirrors"))
        mirror = ET.Element(q("mirror"))
        # Reusing the repo ID lets Maven apply matching credentials from settings.xml.
        ET.SubElement(mirror, q("id")).text = internal_repo.get("id") or "internal-maven"
        ET.SubElement(mirror, q("mirrorOf")).text = "*"
        ET.SubElement(mirror, q("url")).text = internal_repo["url"]
        mirrors.insert(0, mirror)
        settings_path = temp_dir / "settings.xml"
        ET.ElementTree(settings_root).write(settings_path, encoding="utf-8", xml_declaration=True)

    command = ["mvn", "--batch-mode"]
    if offline:
        command.append("--offline")
    if settings_path:
        command.extend(["--settings", str(settings_path)])
    command.extend(["help:effective-pom", "-Doutput=" + str(effective_path)])
    try:
        completed = subprocess.run(
            command,
            cwd=str(pom_path.parent),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0 or not effective_path.is_file():
            return None, (
                "Maven effective-POM resolution failed. Check Maven installation, cached/internally available "
                "plugins and parent POMs, repository connectivity, and credentials."
            )
        resolved = parse_pom(effective_path, pom_path.parent)
        if not resolved.get("dependencies"):
            return None, "Maven generated an effective POM, but no dependency entries were found."
        return resolved, None
    except subprocess.TimeoutExpired:
        return None, "Maven effective-POM resolution timed out after 120 seconds."
    except OSError:
        return None, "Maven could not be started for effective-POM resolution."
    finally:
        temp.cleanup()


def audit(
    project: str | Path,
    resolve: bool = False,
    internal_only: bool = False,
    offline: bool = False,
) -> dict:
    """Audit a Maven project in online, internal-only, or zero-network mode."""
    if internal_only and offline:
        raise ValueError("Choose either --internal-only or --offline, not both.")

    root = Path(project).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("Project path must be an existing directory")

    excluded = {".git", "target", "build", "out", "bin", "node_modules", ".gradle"}
    build_files = [
        path for path in root.rglob("*")
        if path.is_file() and path.name in {"pom.xml", "build.gradle", "build.gradle.kts"}
        and not any(part in excluded for part in path.parts)
    ]
    mode = "OFFLINE" if offline else "INTERNAL_ONLY" if internal_only else "ONLINE"
    if not build_files:
        return {
            "project": {"name": root.name, "project_type": "Unknown"},
            "path": str(root), "timestamp": now_utc(), "fileSummary": count_files_and_types(root),
            "summary": {
                "total_dependencies": 0, "outdated_components": 0, "outdated_technologies": 0,
                "components_needing_review": 0, "status": "NO_BUILD_FILES",
                "message": "No supported build files were found.",
            },
            "checks": {"mode": mode, "latest_versions": "SKIPPED", "lifecycle": "SKIPPED"},
            "outdated_components": [], "outdated_technologies": [], "review_items": [],
        }

    pom_path = next((path for path in sorted(build_files) if path.name == "pom.xml"), None)
    if pom_path is None:
        return {
            "project": {"name": root.name, "project_type": "Gradle"},
            "path": str(root), "timestamp": now_utc(), "fileSummary": count_files_and_types(root),
            "summary": {
                "total_dependencies": 0, "outdated_components": 0, "outdated_technologies": 0,
                "components_needing_review": 0, "status": "CHECK_INCOMPLETE",
                "message": "This version of the auditor currently implements Maven dependency auditing only.",
            },
            "checks": {"mode": mode, "latest_versions": "NOT_SUPPORTED", "lifecycle": "SKIPPED"},
            "outdated_components": [], "outdated_technologies": [], "review_items": [],
        }

    data = parse_pom(pom_path, root)
    project_info = get_project_info(pom_path, root, data["java"])
    repositories = data.get("repositories", [])
    if internal_only:
        repositories = [
            repo for repo in repositories
            if is_internal_repository_url(repo["url"], repo["id"])
        ]

    effective_pom_status = "NOT_REQUESTED"
    effective_pom_message = None
    if resolve:
        resolved, error_message = resolve_effective_pom(
            pom_path, repositories, internal_only=internal_only, offline=offline
        )
        if resolved is not None:
            data["dependencies"] = resolved.get("dependencies", data["dependencies"])
            data["java"] = data.get("java") or resolved.get("java")
            data["boot"] = data.get("boot") or resolved.get("boot")
            effective_pom_status = "RESOLVED"
        else:
            effective_pom_status = "FAILED"
            effective_pom_message = error_message

    latest_checked = latest_failed = latest_skipped = 0
    outdated_components: list[dict] = []
    outdated_technologies: list[dict] = []
    review_items: list[dict] = []

    def check_latest(group: str, artifact: str) -> dict:
        return latest_maven_version(
            group, artifact, repository_urls=repositories,
            internal_only=internal_only, offline=offline,
        )

    for dep in data["dependencies"]:
        group, artifact, version = dep["group_id"], dep["artifact_id"], dep["version"]
        coordinate = f"{group}:{artifact}"
        if not version:
            latest_skipped += 1
            review_items.append({
                "component": coordinate, "name": artifact, "current_version": None,
                "status": "VERSION_UNRESOLVED",
                "reason": "No version is declared directly or in this POM's dependencyManagement; a parent POM or imported BOM may manage it.",
                "action": "Run with --resolve to inspect Maven's effective POM.",
            })
            continue
        if offline:
            latest_skipped += 1
            continue

        latest = check_latest(group, artifact)
        if latest.get("status") == "FOUND" and latest.get("version"):
            latest_checked += 1
            latest_version = latest["version"]
            if compare_versions(version, latest_version) < 0:
                outdated_components.append({
                    "module": "backend", "name": artifact, "current_version": version,
                    "latest_version": latest_version, "component": coordinate,
                    "reason": "Newer stable release available",
                    "explanation": f"The current version ({version}) is older than the latest stable release ({latest_version}) in {latest.get('source', 'the configured repository')}. Check compatibility before updating.",
                    "status": "OUTDATED", "action": f"Update to {latest_version} and verify compatibility",
                    "source_url": latest.get("url"), "source": latest.get("source"),
                })
        elif latest.get("status") in {"SKIPPED_NO_INTERNAL_REPOSITORY", "SKIPPED_OFFLINE"}:
            latest_skipped += 1
            if internal_only:
                review_items.append({
                    "component": coordinate, "name": artifact, "current_version": version,
                    "status": "VERSION_CHECK_SKIPPED",
                    "reason": latest.get("message", "No internal repository is configured for this lookup."),
                    "action": "Declare an approved internal Maven repository in the project's POM.",
                })
        else:
            latest_failed += 1
            review_items.append({
                "component": coordinate, "name": artifact, "current_version": version,
                "status": "VERSION_CHECK_INCOMPLETE",
                "reason": latest.get("message") or "Latest-version metadata could not be retrieved.",
                "action": "Check repository availability, permissions, and metadata.",
            })

    if data.get("boot"):
        if offline:
            latest_skipped += 1
        else:
            boot_latest = check_latest("org.springframework.boot", "spring-boot-starter-parent")
            if boot_latest.get("status") == "FOUND" and boot_latest.get("version"):
                latest_checked += 1
                if compare_versions(data["boot"], boot_latest["version"]) < 0:
                    outdated_technologies.append({
                        "module": "backend", "name": "Spring Boot", "current_version": data["boot"],
                        "latest_version": boot_latest["version"], "component": "Spring Boot",
                        "reason": "A newer stable Spring Boot parent version is available in " + boot_latest.get("source", "the configured repository"),
                        "explanation": "Review Spring Boot release notes and Java compatibility before upgrading.",
                        "status": "OUTDATED", "action": f"Review an upgrade to {boot_latest['version']}",
                        "source_url": boot_latest.get("url"), "source": boot_latest.get("source"),
                    })
            elif boot_latest.get("status") in {"SKIPPED_NO_INTERNAL_REPOSITORY", "SKIPPED_OFFLINE"}:
                latest_skipped += 1
                if internal_only:
                    review_items.append({
                        "component": "Spring Boot", "name": "Spring Boot", "current_version": data["boot"],
                        "status": "VERSION_CHECK_SKIPPED",
                        "reason": boot_latest.get("message", "No approved internal repository is available."),
                        "action": "Declare an approved internal Maven repository in the project's POM.",
                    })
            else:
                latest_failed += 1
                review_items.append({
                    "component": "Spring Boot", "name": "Spring Boot", "current_version": data["boot"],
                    "status": "VERSION_CHECK_INCOMPLETE",
                    "reason": boot_latest.get("message") or "Spring Boot parent metadata could not be retrieved.",
                    "action": "Check repository availability, permissions, and metadata.",
                })

    lifecycle_checked = lifecycle_failed = lifecycle_skipped = 0
    for product, version in (("java", data.get("java")), ("spring-boot", data.get("boot"))):
        if not version:
            continue
        life = lifecycle_status(product, version, allow_external=not (internal_only or offline))
        if life["status"] in {"EOL", "NOT_MAINTAINED"}:
            lifecycle_checked += 1
            outdated_technologies.append({
                "module": "backend", "name": "Java" if product == "java" else "Spring Boot",
                "current_version": version, "latest_version": None,
                "component": "Java" if product == "java" else "Spring Boot",
                "reason": "Lifecycle status: " + life["status"],
                "explanation": f"{product} {version} lifecycle status was identified by the configured lifecycle source.",
                "status": life["status"],
                "action": "Review the vendor's support policy and plan an upgrade if needed.",
                "source_url": life.get("source_url"),
            })
        elif life["status"] == "MAINTAINED":
            lifecycle_checked += 1
        elif life["status"] == "UNAVAILABLE":
            lifecycle_failed += 1
        else:
            lifecycle_skipped += 1

    # Locally encoded maintenance advisories work in every mode without HTTP requests.
    for dep in data["dependencies"]:
        advisory = known_deprecation(dep["group_id"], dep["artifact_id"])
        if advisory.get("status") == "MAINTENANCE_MODE":
            outdated_components.append({
                "module": "backend", "name": dep["artifact_id"], "current_version": dep.get("version"),
                "latest_version": advisory.get("recommended_alternative"),
                "component": f"{dep['group_id']}:{dep['artifact_id']}",
                "reason": advisory["reason"], "explanation": advisory["reason"],
                "status": "MAINTENANCE_MODE", "action": advisory["recommended_alternative"],
                "source_url": None if internal_only or offline else advisory.get("source_url"),
            })

    version_status = (
        "SKIPPED" if offline or (latest_checked == 0 and latest_failed == 0)
        else "PARTIAL" if latest_failed or latest_skipped else "CHECKED"
    )
    lifecycle_status_value = (
        "SKIPPED" if internal_only or offline
        else "PARTIAL" if lifecycle_failed
        else "CHECKED" if lifecycle_checked else "NOT_APPLICABLE"
    )

    if outdated_components or outdated_technologies:
        overall_status = "UPDATES_REQUIRED"
        message = "One or more components or technologies need attention."
    elif version_status in {"PARTIAL", "SKIPPED"} or lifecycle_status_value in {"PARTIAL", "SKIPPED"} or review_items or effective_pom_status == "FAILED":
        overall_status = "CHECK_INCOMPLETE"
        message = "No confirmed update was found for every component; review incomplete or skipped checks before treating the project as current."
    else:
        overall_status = "ALL_CURRENT"
        message = "All components covered by the completed checks appear current."

    return {
        "project": project_info, "path": str(root), "timestamp": now_utc(),
        "fileSummary": count_files_and_types(root),
        "summary": {
            "total_dependencies": len(data["dependencies"]),
            "outdated_components": len(outdated_components),
            "outdated_technologies": len(outdated_technologies),
            "components_needing_review": len(review_items),
            "status": overall_status, "message": message,
        },
        "checks": {
            "mode": mode,
            "latest_versions": version_status,
            "latest_versions_checked": latest_checked,
            "latest_versions_failed": latest_failed,
            "latest_versions_skipped": latest_skipped,
            "lifecycle": lifecycle_status_value,
            "lifecycle_checked": lifecycle_checked,
            "lifecycle_failed": lifecycle_failed,
            "lifecycle_skipped": lifecycle_skipped,
            "internal_repositories_detected": len(repositories) if internal_only else 0,
            "effective_pom": effective_pom_status,
            "effective_pom_message": effective_pom_message,
        },
        "outdated_components": outdated_components,
        "outdated_technologies": outdated_technologies,
        "review_items": review_items,
    }


def main():
    parser = argparse.ArgumentParser(description="Audit Maven dependency versions and support status.")
    parser.add_argument("project", help="Path to Java project")
    parser.add_argument("-o", "--output", default="audit-report.json", help="Output JSON file")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--internal-only", action="store_true",
        help="Query only private/local Maven repositories declared in pom.xml; skip public lifecycle APIs",
    )
    mode_group.add_argument(
        "--offline", action="store_true",
        help="Make no network requests; perform static checks only",
    )
    parser.add_argument(
        "--resolve", action="store_true",
        help="Ask Maven for the effective POM to resolve parent/BOM-managed versions",
    )
    args = parser.parse_args()

    try:
        report = audit(
            args.project,
            resolve=args.resolve,
            internal_only=args.internal_only,
            offline=args.offline,
        )
    except ValueError as error:
        parser.error(str(error))

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = report.get("summary", {})
    checks = report.get("checks", {})
    project = report.get("project", {})
    project_name = project.get("name", "Unknown") if isinstance(project, dict) else project
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
    print("Report saved to: " + str(output))


if __name__ == "__main__":
    main()
