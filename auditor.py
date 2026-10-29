from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Java versions below this baseline are flagged as needing changes.
MINIMUM_JAVA_VERSION = "8"
HTTP_TIMEOUT = 3
TOOL_TIMEOUT = 60
SKIP_DIRS = {".git", "target", "build", "out", "bin", "node_modules", ".gradle", "__pycache__", ".venv", "venv"}
BUILD_NAMES = {"pom.xml", "build.gradle", "build.gradle.kts", "build.xml"}
SETTINGS_NAMES = {"settings.gradle", "settings.gradle.kts"}
USER_AGENT = "Java-Version-Auditor/3.0"
PLACEHOLDER_START = "$" + "{"
PRE_RELEASE = re.compile(r"(?i)(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea|cr)(?:[.-]?\d*)")

FRAMEWORK_RULES = [
    ("Spring Boot", [("org.springframework.boot", "spring-boot"), ("org.springframework.boot", "spring-boot-autoconfigure")]),
    ("Spring MVC", [("org.springframework", "spring-webmvc")]),
    ("Spring Framework", [("org.springframework", "spring-core")]),
    ("Spring Security", [("org.springframework.security", "spring-security-core"), ("org.springframework.security", "spring-security-web"), ("org.springframework.security", "spring-security-crypto")]),
    ("Hibernate", [("org.hibernate", "hibernate-core"), ("org.hibernate.orm", "hibernate-core")]),
    ("Struts", [("org.apache.struts", "struts2-core"), ("org.apache.struts", "struts-core")]),
    ("JSF", [("org.glassfish", "jakarta.faces"), ("org.glassfish", "javax.faces"), ("com.sun.faces", "jsf-impl"), ("javax.faces", "jsf-api"), ("jakarta.faces", "jakarta.faces-api")]),
    ("Apache Camel", [("org.apache.camel", "camel-core-engine"), ("org.apache.camel", "camel-core")]),
    ("JPA", [("jakarta.persistence", "jakarta.persistence-api"), ("javax.persistence", "javax.persistence-api"), ("org.hibernate.javax.persistence", "hibernate-jpa-2.1-api")]),
]

DEPRECATED_API_RULES = [
    (re.compile(r"\bsun\.misc\."), "sun.misc API", "Internal JDK API; direct use can be fragile on newer JDKs."),
    (re.compile(r"\bjavax\.xml\.bind\."), "JAXB API", "JAXB is no longer bundled with the JDK from Java 11 onward; add a supported dependency if needed."),
    (re.compile(r"\bSecurityManager\b"), "SecurityManager", "SecurityManager is deprecated for removal in modern Java releases."),
    (re.compile(r"\bjava\.util\.Observable\b|\bjava\.util\.Observer\b"), "Observable/Observer", "This API is deprecated; consider a modern event mechanism."),
]


def local_name(node):
    return node.tag.split("}")[-1]


def child(node, name):
    return next((x for x in list(node or []) if local_name(x) == name), None)


def node_text(node, name):
    item = child(node, name)
    return (item.text or "").strip() if item is not None and item.text else None


def clean(value):
    if value is None:
        return None
    value = str(value).strip().strip("'\"")
    return value or None


def interpolate(value, properties):
    if not value:
        return None
    result = value.strip()
    for _ in range(8):
        updated = re.sub(r"\$\{([^}]+)\}", lambda m: properties.get(m.group(1), m.group(0)), result)
        if updated == result:
            break
        result = updated
    return None if PLACEHOLDER_START in result else clean(result)


def java_major(value):
    value = clean(value)
    if not value:
        return None
    if value.startswith("1.") and len(value.split(".")) > 1:
        return value.split(".")[1]
    match = re.match(r"\d+", value)
    return match.group(0) if match else value


def version_tuple(value):
    match = re.match(r"^\s*[vV]?(\d+(?:\.\d+)*)", str(value or ""))
    if not match:
        return ()
    nums = [int(piece) for piece in match.group(1).split(".")]
    return tuple((nums + [0] * 8)[:8])


def compare_versions(left, right):
    a, b = version_tuple(left), version_tuple(right)
    if not a or not b:
        return 0
    if a != b:
        return (a > b) - (a < b)
    left_pre = bool(PRE_RELEASE.search(str(left or "")))
    right_pre = bool(PRE_RELEASE.search(str(right or "")))
    return (right_pre > left_pre) - (left_pre > right_pre)


def version_sort_key(value):
    return version_tuple(value) + (0 if PRE_RELEASE.search(str(value or "")) else 1,)


def rel(path, root):
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/") or "."
    except Exception:
        return str(path).replace("\\", "/")


def should_skip(path, root):
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except Exception:
        parts = path.parts
    return any(part in SKIP_DIRS for part in parts)


def resolve_path_expression(value, basedir, properties=None):
    if not value:
        return None
    value = interpolate(value, properties or {}) or value
    value = value.replace("$" + "{project.basedir}", str(basedir)).replace("$" + "{basedir}", str(basedir))
    if PLACEHOLDER_START in value:
        return None
    path = Path(value)
    return path if path.is_absolute() else basedir / path


def read_pom(pom, root):
    info = {"path": pom, "java_versions": [], "boot_versions": [], "direct_dependencies": [], "project_coordinates": None, "missing_files": [], "error": None, "is_aggregator": False}
    try:
        document = ET.parse(pom).getroot()
        info["is_aggregator"] = (node_text(document, "packaging") or "jar") == "pom" and child(document, "modules") is not None
    except Exception as exc:
        info["error"] = str(exc)
        return info

    props_node = child(document, "properties")
    properties = {local_name(x): (x.text or "").strip() for x in list(props_node or []) if x.text}
    for key in ("java.version", "maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "jdk.version"):
        if key in properties:
            val = java_major(interpolate(properties[key], properties))
            if val:
                info["java_versions"].append({"version": val, "source": f"{rel(pom, root)}:properties.{key}"})

    for plugin in document.iter():
        if local_name(plugin) != "plugin" or node_text(plugin, "artifactId") != "maven-compiler-plugin":
            continue
        config = child(plugin, "configuration")
        for key in ("release", "source", "target"):
            resolved = java_major(interpolate(node_text(config, key), properties))
            if resolved:
                info["java_versions"].append({"version": resolved, "source": f"{rel(pom, root)}:maven-compiler-plugin.{key}"})

    group_id, artifact_id, version = node_text(document, "groupId"), node_text(document, "artifactId"), node_text(document, "version")
    parent = child(document, "parent")
    if not group_id and parent is not None:
        group_id = node_text(parent, "groupId")
    info["project_coordinates"] = (group_id, artifact_id, version)

    if parent is not None and node_text(parent, "groupId") == "org.springframework.boot" and node_text(parent, "artifactId") == "spring-boot-starter-parent":
        boot_version = interpolate(node_text(parent, "version"), properties)
        if boot_version:
            info["boot_versions"].append({"version": boot_version, "source": f"{rel(pom, root)}:parent.version"})

    managed = {}
    dm = child(document, "dependencyManagement")
    dm_deps = child(dm, "dependencies")
    for dep in list(dm_deps or []):
        if local_name(dep) != "dependency":
            continue
        g, a = node_text(dep, "groupId"), node_text(dep, "artifactId")
        v = interpolate(node_text(dep, "version"), properties)
        if g and a and v:
            managed[(g, a)] = v
        if g == "org.springframework.boot" and a == "spring-boot-dependencies" and v:
            info["boot_versions"].append({"version": v, "source": f"{rel(pom, root)}:dependencyManagement.spring-boot-dependencies"})

    for dep in list(child(document, "dependencies") or []):
        if local_name(dep) != "dependency":
            continue
        g, a = node_text(dep, "groupId"), node_text(dep, "artifactId")
        if not g or not a:
            continue
        raw_version = node_text(dep, "version")
        resolved = interpolate(raw_version, properties) if raw_version else managed.get((g, a))
        entry = {
            "group_id": g, "artifact_id": a, "version": resolved,
            "scope": node_text(dep, "scope") or "compile", "direct": True,
            "version_source": "property/direct declaration" if raw_version else ("local dependencyManagement" if resolved else "inherited or BOM-managed"),
            "module": rel(pom.parent, root), "source": rel(pom, root),
        }
        info["direct_dependencies"].append(entry)

        system_path = node_text(dep, "systemPath")
        if system_path:
            target = resolve_path_expression(system_path, pom.parent, properties)
            if target is not None and not target.exists():
                info["missing_files"].append({"path": rel(target, root), "referenced_by": rel(pom, root), "reason": f"Dependency {g}:{a} declares a systemPath that does not exist."})

    if parent is not None:
        relative = child(parent, "relativePath")
        if relative is not None and (relative.text or "").strip():
            target = resolve_path_expression(relative.text.strip(), pom.parent, properties)
            if target is not None and not target.exists():
                info["missing_files"].append({"path": rel(target, root), "referenced_by": rel(pom, root), "reason": "The POM's explicitly configured parent relativePath does not exist."})

    for module_node in list(child(document, "modules") or []):
        if local_name(module_node) != "module" or not module_node.text:
            continue
        value = interpolate(module_node.text.strip(), properties)
        if not value:
            continue
        target = (pom.parent / value).resolve()
        if not target.exists():
            info["missing_files"].append({"path": rel(target, root), "referenced_by": rel(pom, root), "reason": "The POM lists this path as a Maven module, but it does not exist."})
        elif target.is_dir() and not (target / "pom.xml").exists():
            info["missing_files"].append({"path": rel(target / "pom.xml", root), "referenced_by": rel(pom, root), "reason": "The declared Maven module directory exists but has no pom.xml."})

    for node in document.iter():
        if local_name(node) == "resource":
            target = resolve_path_expression(node_text(node, "directory"), pom.parent, properties)
            if target is not None and not target.exists():
                info["missing_files"].append({"path": rel(target, root), "referenced_by": rel(pom, root), "reason": "A configured Maven resource directory does not exist."})
    return info


def read_gradle(build_file, root):
    text = build_file.read_text(encoding="utf-8", errors="replace")
    module = rel(build_file.parent, root)
    info = {"path": build_file, "java_versions": [], "boot_versions": [], "direct_dependencies": [], "project_coordinates": None, "missing_files": [], "error": None}

    patterns = (
        r"(?:sourceCompatibility|targetCompatibility)\s*[=:]\s*['\"]?(?:JavaVersion\.)?VERSION_(?:1_)?(\d+)",
        r"(?:sourceCompatibility|targetCompatibility)\s*[=:]\s*['\"]?(1\.\d+|\d+)",
        r"JavaLanguageVersion\.of\(\s*['\"]?(\d+)['\"]?\s*\)",
        r"(?:maven\.compiler\.(?:source|target|release)|java\.version)\s*[=:]\s*['\"]?(\d+(?:\.\d+)*)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            raw = next((v for v in match.groups() if v), None)
            version = java_major(raw)
            if version:
                info["java_versions"].append({"version": version, "source": f"{rel(build_file, root)}:Java configuration"})

    boot_patterns = (
        r"id\s*\(?\s*['\"]org\.springframework\.boot['\"]\s*\)?\s*version\s*['\"]([^'\"]+)['\"]",
        r"org\.springframework\.boot:spring-boot-gradle-plugin:([0-9][^'\"\s]+)",
        r"springBootVersion\s*=\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in boot_patterns:
        for match in re.finditer(pattern, text, re.I):
            info["boot_versions"].append({"version": clean(match.group(1)), "source": f"{rel(build_file, root)}:Spring Boot plugin/version"})

    dep_pattern = re.compile(r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor|classpath)\s*\(?\s*['\"]([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([^'\"\s)]+)['\"]")
    for match in dep_pattern.finditer(text):
        info["direct_dependencies"].append({
            "group_id": match.group(1), "artifact_id": match.group(2), "version": clean(match.group(3)),
            "scope": "gradle", "direct": True, "version_source": "Gradle declaration",
            "module": module, "source": rel(build_file, root),
        })
    return info


def read_ant(build_file, root):
    info = {"path": build_file, "java_versions": [], "boot_versions": [], "direct_dependencies": [], "project_coordinates": None, "missing_files": [], "error": None}
    properties = build_file.with_name("build.properties")
    if properties.exists():
        for line_no, line in enumerate(properties.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "=" not in line:
                continue
            key, value = [piece.strip() for piece in line.split("=", 1)]
            if key in {"java.version", "javaVersion", "jdk.version"}:
                version = java_major(value)
                if version:
                    info["java_versions"].append({"version": version, "source": f"{rel(properties, root)}:{line_no}"})
            if key in {"spring-boot.version", "spring.boot.version", "springBootVersion"}:
                info["boot_versions"].append({"version": clean(value), "source": f"{rel(properties, root)}:{line_no}"})
    return info


def find_files(root, names):
    return sorted((p for p in root.rglob("*") if p.is_file() and p.name in names and not should_skip(p, root)), key=lambda p: str(p).lower())


def executable_or_wrapper(root, system, parent=None):
    found = shutil.which(system)
    if found:
        return [found]
    search_dirs = [parent, root, *root.parents]
    names = (["mvnw.cmd", "mvnw"] if os.name == "nt" else ["mvnw", "mvnw.cmd"]) if system == "mvn" else (["gradlew.bat", "gradlew"] if os.name == "nt" else ["gradlew", "gradlew.bat"])
    seen = set()
    for folder in search_dirs:
        if folder is None:
            continue
        folder = Path(folder)
        if folder in seen:
            continue
        seen.add(folder)
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                if candidate.suffix.lower() in {".cmd", ".bat"} or os.access(candidate, os.X_OK):
                    return [str(candidate)]
                if candidate.name in {"mvnw", "gradlew"}:
                    return ["sh", str(candidate)]
    return None


def run_command(command, cwd, timeout):
    try:
        if os.name == "nt" and command[0].lower().endswith((".cmd", ".bat")):
            result = subprocess.run(subprocess.list2cmdline(command), cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout, errors="replace")
        else:
            result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, errors="replace")
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout} seconds."
    except Exception as exc:
        return 127, "", str(exc)


MAVEN_COORD = re.compile(
    r"(?P<group>[A-Za-z0-9_.-]+):(?P<artifact>[A-Za-z0-9_.-]+):(?P<type>[A-Za-z0-9_.-]+):"
    r"(?:(?P<classifier>[A-Za-z0-9_.-]+):)?(?P<version>[^:\s]+):"
    r"(?P<scope>compile|runtime|test|provided|system|import)(?=$|\s|\))"
)
GRADLE_COORD = re.compile(r"(?P<group>[A-Za-z0-9_.-]+):(?P<artifact>[A-Za-z0-9_.-]+):(?P<version>[A-Za-z0-9_.+_-]+)")


def parse_maven_tree(output, info, root):
    found = []
    project_coords = info.get("project_coordinates") or (None, None, None)
    for line in output.splitlines():
        if "omitted for conflict" in line.lower() or "omitted for duplicate" in line.lower():
            continue
        match = MAVEN_COORD.search(line)
        if not match:
            continue
        g, a, v, scope = match.group("group", "artifact", "version", "scope")
        if (g, a) == project_coords[:2] or not v or PLACEHOLDER_START in v:
            continue
        found.append({
            "group_id": g, "artifact_id": a, "version": v, "scope": scope,
            "direct": False, "version_source": "resolved Maven dependency tree",
            "module": rel(info["path"].parent, root), "source": rel(info["path"], root),
        })
    return found


def parse_gradle_tree(output, info, root):
    found = []
    for line in output.splitlines():
        if "FAILED" in line or "No dependencies" in line:
            continue
        match = GRADLE_COORD.search(line)
        if not match:
            continue
        g, a, version = match.group("group", "artifact", "version")
        arrow = re.search(r"\s->\s*([^\s]+)", line)
        if arrow:
            version = arrow.group(1)
        if version in {"unspecified", "FAILED"} or PLACEHOLDER_START in version:
            continue
        found.append({
            "group_id": g, "artifact_id": a, "version": version, "scope": "runtimeClasspath",
            "direct": False, "version_source": "resolved Gradle dependency report",
            "module": rel(info["path"].parent, root), "source": rel(info["path"], root),
        })
    return found


def parse_versions_plugin_output(output):
    """Parse current -> available versions from Maven Versions Plugin output."""
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    line_pattern = re.compile(
        r"(?P<coordinate>[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+)\s+\.{2,}\s+"
        r"(?P<current>\S+)\s*->\s*(?P<available>.+?)\s*$"
    )
    updates = {}
    for raw_line in output.splitlines():
        line = ansi.sub("", raw_line).strip()
        match = line_pattern.search(line)
        if not match:
            continue
        group, artifact = match.group("coordinate").split(":", 1)
        current = match.group("current").strip().rstrip(",")
        candidates = []
        for value in match.group("available").split(","):
            candidate = value.strip().split()[0].rstrip(",;") if value.strip() else ""
            if candidate and version_tuple(candidate) and not PRE_RELEASE.search(candidate):
                candidates.append(candidate)
        if not candidates:
            continue
        versions = sorted(set([current] + candidates), key=version_sort_key)
        key = (group, artifact)
        if key in updates:
            versions = sorted(set(versions + updates[key]["versions"]), key=version_sort_key)
        updates[key] = {
            "status": "FOUND",
            "versions": versions,
            "latest_version": versions[-1],
            "source": "Maven Versions Plugin",
        }
    return updates


def resolve_maven_updates(info, root):
    """Use Maven's Versions Plugin as a fallback for release-version lookup."""
    launcher = executable_or_wrapper(root, "mvn", info["path"].parent)
    if not launcher:
        return {}, "Maven executable or wrapper not found for version lookup"

    plugin_version = "2.21.0"
    command = launcher + [
        "-B", "-f", str(info["path"].resolve()),
        "-Dstyle.color=never",
        "-Dversions.showNewestPerSegment=true",
        "-DshowVersionless=true",
        "-Dverbose=false",
        f"org.codehaus.mojo:versions-maven-plugin:{plugin_version}:display-dependency-updates",
        f"org.codehaus.mojo:versions-maven-plugin:{plugin_version}:display-parent-updates",
    ]
    code, stdout, stderr = run_command(command, root, TOOL_TIMEOUT)
    parsed = parse_versions_plugin_output((stdout or "") + "\n" + (stderr or ""))
    if parsed:
        return parsed, None
    if code == 0:
        return {}, None
    lines = (stderr.strip() or stdout.strip() or f"Maven Versions Plugin exited with code {code}").splitlines()
    return {}, lines[-1][:220] if lines else "Maven Versions Plugin could not check newer versions"


def resolve_dependency_tree(info, root):
    system = info.get("build_system")
    if system == "Maven":
        launcher = executable_or_wrapper(root, "mvn", info["path"].parent)
        if not launcher:
            return [], "Maven executable or Maven wrapper not found"
        command = launcher + ["-B", "-f", str(info["path"].resolve()), "dependency:tree", "-DoutputType=text", "-Dstyle.color=never"]
        code, stdout, stderr = run_command(command, root, TOOL_TIMEOUT)
        if code:
            lines = (stderr.strip() or stdout.strip() or f"Maven exited with code {code}").splitlines()
            return [], lines[-1][:220] if lines else "Maven dependency tree could not be resolved"
        result = parse_maven_tree(stdout, info, root)
        if result or not info["direct_dependencies"]:
            return result, None
        return [], "Maven ran, but no dependency coordinates were parsed"
    if system == "Gradle":
        launcher = executable_or_wrapper(root, "gradle", info["path"].parent)
        if not launcher:
            return [], "Gradle executable or Gradle wrapper not found"
        command = launcher + ["-q", "-p", str(info["path"].parent.resolve()), "dependencies", "--configuration", "runtimeClasspath"]
        code, stdout, stderr = run_command(command, root, TOOL_TIMEOUT)
        if code:
            lines = (stderr.strip() or stdout.strip() or f"Gradle exited with code {code}").splitlines()
            return [], lines[-1][:220] if lines else "Gradle dependency report could not be resolved"
        result = parse_gradle_tree(stdout, info, root)
        if result or not info["direct_dependencies"]:
            return result, None
        return [], "Gradle ran, but no resolved dependency coordinates were parsed"
    return [], "Ant dependency resolution is not supported"


def merge_dependencies(info, tree_deps):
    direct = {(d["group_id"], d["artifact_id"]): d for d in info["direct_dependencies"]}
    merged = {}
    resolved_coordinates = set()
    for dep in tree_deps:
        item = dict(dep)
        coordinate = (dep["group_id"], dep["artifact_id"])
        resolved_coordinates.add(coordinate)
        declaration = direct.get(coordinate)
        if declaration:
            item["direct"] = True
            item["version_source"] = declaration["version_source"]
            item["scope"] = declaration.get("scope") or item["scope"]
            item["source"] = declaration["source"]
        merged[(item["group_id"], item["artifact_id"], item.get("version"))] = item
    for dep in info["direct_dependencies"]:
        coordinate = (dep["group_id"], dep["artifact_id"])
        if coordinate in resolved_coordinates:
            # The resolved tree supplies the effective version; do not add a duplicate null-version row.
            continue
        key = (dep["group_id"], dep["artifact_id"], dep.get("version"))
        if key not in merged:
            merged[key] = dict(dep)
    return sorted(merged.values(), key=lambda d: (d["group_id"].lower(), d["artifact_id"].lower(), str(d.get("version") or "")))


def maven_metadata(group, artifact):
    path = "/".join(urllib.parse.quote(piece, safe="") for piece in group.split("."))
    artifact_path = urllib.parse.quote(artifact, safe="")
    url = f"https://repo.maven.apache.org/maven2/{path}/{artifact_path}/maven-metadata.xml"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            xml = response.read()
        root = ET.fromstring(xml)
        versioning = child(root, "versioning")
        version_node = child(versioning, "versions")
        versions = [(x.text or "").strip() for x in list(version_node or []) if x.text]
        stable = sorted({v for v in versions if not PRE_RELEASE.search(v)}, key=version_sort_key)
        if not stable:
            release = node_text(versioning, "release")
            stable = [release] if release and not PRE_RELEASE.search(release) else []
        return {"status": "FOUND", "versions": stable, "latest_version": stable[-1], "source": url} if stable else {"status": "NO_STABLE_RELEASE", "versions": [], "source": url}
    except urllib.error.HTTPError as exc:
        return {"status": "NOT_FOUND" if exc.code == 404 else "UNAVAILABLE", "versions": [], "source": url}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "versions": [], "source": url, "message": str(exc)[:140]}


def version_advice(current, metadata):
    versions = list(metadata.get("versions", []) or [])
    metadata_latest = metadata.get("latest_version")
    if metadata_latest and metadata_latest not in versions:
        versions.append(metadata_latest)
    versions = sorted({v for v in versions if version_tuple(v)}, key=version_sort_key)

    if not current or not version_tuple(current):
        return {
            "latest_version": metadata_latest or "Unavailable",
            "recommended_version": "Resolve version",
            "version_status": "NEEDS_CHANGES",
        }
    if not versions:
        return {
            "latest_version": "Unavailable",
            "recommended_version": "Unavailable",
            "version_status": "CHECK_UNAVAILABLE",
        }

    latest = versions[-1]
    current_major = version_tuple(current)[0]
    same_major = [v for v in versions if version_tuple(v) and version_tuple(v)[0] == current_major]
    same_major_latest = same_major[-1] if same_major else None

    if same_major_latest and compare_versions(current, same_major_latest) < 0:
        return {"latest_version": latest, "recommended_version": same_major_latest, "version_status": "NEEDS_UPDATE"}
    if compare_versions(current, latest) < 0:
        return {"latest_version": latest, "recommended_version": latest, "version_status": "MAJOR_UPDATE_AVAILABLE"}
    return {"latest_version": latest, "recommended_version": current, "version_status": "CURRENT"}


def fetch_metadata_for(coords):
    cache = {}
    coords = sorted(set(coords))
    if not coords:
        return cache
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(coords))) as pool:
        futures = {pool.submit(maven_metadata, g, a): (g, a) for g, a in coords}
        for future in concurrent.futures.as_completed(futures):
            coordinate = futures[future]
            try:
                cache[coordinate] = future.result()
            except Exception:
                cache[coordinate] = {"status": "UNAVAILABLE", "versions": []}
    return cache


def identify_frameworks(deps, info, metadata_cache, root):
    result = []
    candidates = {}
    for name, coords in FRAMEWORK_RULES:
        found = []
        for group, artifact in coords:
            for dep in deps:
                if dep["group_id"] == group and dep["artifact_id"] == artifact and dep.get("version"):
                    found.append({"version": dep["version"], "component": f"{group}:{artifact}", "source": dep.get("source"), "module": dep.get("module")})
        if name == "Spring Boot":
            for hint in info.get("boot_versions", []):
                if hint.get("version"):
                    found.append({"version": hint["version"], "component": "org.springframework.boot:spring-boot-starter-parent", "source": hint["source"], "module": rel(info["path"].parent, root)})
        if not found:
            # Recognize split modules and framework starters without inventing their versions.
            fallback_groups = {
                "Spring Framework": {"org.springframework"},
                "Spring Security": {"org.springframework.security"},
                "Apache Camel": {"org.apache.camel"},
            }
            allowed = fallback_groups.get(name, set())
            if allowed:
                found = [{"version": d["version"], "component": f"{d['group_id']}:{d['artifact_id']}", "source": d.get("source"), "module": d.get("module")} for d in deps if d["group_id"] in allowed and d.get("version")]
        if not found:
            continue

        distinct = sorted({x["version"] for x in found}, key=version_sort_key)
        selected = next((x for x in found if x["version"] == distinct[-1]), found[0])
        if name == "Spring Boot":
            lookup = ("org.springframework.boot", "spring-boot-starter-parent")
        else:
            lookup = tuple(selected["component"].split(":", 1))
        metadata = metadata_cache.get(lookup, {"versions": [], "status": "SKIPPED"})
        advice = version_advice(selected["version"], metadata)
        result.append({
            "name": name,
            "version": distinct[0] if len(distinct) == 1 else "Multiple",
            "component": selected["component"],
            "module": selected.get("module") or rel(info["path"].parent, root),
            "source": selected.get("source"),
            **advice,
        })
    return result


def detect_missing_gradle_modules(settings_files, root):
    missing = []
    include_pattern = re.compile(r"include\s*\(?([^\n)]*)\)?")
    project_dir_pattern = re.compile(r"project\s*\(\s*['\"](:[^'\"]+)['\"]\s*\)\.projectDir\s*=\s*file\(['\"]([^'\"]+)['\"]\)")
    for settings in settings_files:
        text = settings.read_text(encoding="utf-8", errors="replace")
        explicit = {m.group(1): m.group(2) for m in project_dir_pattern.finditer(text)}
        for match in include_pattern.finditer(text):
            for project_path in re.findall(r"['\"](:[^'\"]+)['\"]", match.group(1)):
                configured = explicit.get(project_path)
                folder = (settings.parent / configured).resolve() if configured else (settings.parent / project_path.lstrip(":").replace(":", "/")).resolve()
                if not folder.exists():
                    missing.append({"path": rel(folder, root), "referenced_by": rel(settings, root), "reason": f"Gradle settings include {project_path}, but its project directory does not exist."})
                elif not any((folder / name).exists() for name in ("build.gradle", "build.gradle.kts")):
                    missing.append({"path": rel(folder / "build.gradle", root), "referenced_by": rel(settings, root), "reason": f"Gradle settings include {project_path}, but the project has no build.gradle or build.gradle.kts."})
    return missing


def scan_deprecated_apis(root):
    findings = []
    for path in root.rglob("*.java"):
        if should_skip(path, root):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, 1):
            for pattern, name, reason in DEPRECATED_API_RULES:
                if pattern.search(line):
                    findings.append({"title": f"Deprecated/legacy API: {name}", "severity": "MEDIUM", "path": rel(path, root), "line": line_no, "evidence": line.strip()[:200], "message": reason})
    return findings


def choose_java(info):
    versions = [item for item in info.get("java_versions", []) if item.get("version")]
    distinct = sorted({item["version"] for item in versions}, key=version_sort_key)
    if not distinct:
        return "Not detected"
    return distinct[0] if len(distinct) == 1 else "Multiple"


def build_report(project_path):
    root = Path(project_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Project path must be an existing directory: {root}")

    build_files = find_files(root, BUILD_NAMES)
    settings_files = find_files(root, SETTINGS_NAMES)
    systems = sorted({
        "Maven" if p.name == "pom.xml" else "Gradle" if p.name.startswith(("build.gradle", "build.gradle.kts")) else "Ant"
        for p in build_files
    })
    if settings_files and "Gradle" not in systems:
        systems.append("Gradle")
        systems.sort()

    module_infos = []
    for build_file in build_files:
        if build_file.name == "pom.xml":
            info = read_pom(build_file, root)
            info["build_system"] = "Maven"
        elif build_file.name.startswith(("build.gradle", "build.gradle.kts")):
            info = read_gradle(build_file, root)
            info["build_system"] = "Gradle"
        else:
            info = read_ant(build_file, root)
            info["build_system"] = "Ant"
        module_infos.append(info)

    if not module_infos and settings_files:
        module_infos.append({"path": settings_files[0], "java_versions": [], "boot_versions": [], "direct_dependencies": [], "project_coordinates": None, "missing_files": [], "error": None, "build_system": "Gradle"})

    missing_files = []
    issues = []
    if not build_files:
        missing_files.append({"path": "pom.xml or build.gradle/build.gradle.kts or build.xml", "referenced_by": ".", "reason": "No supported Java build file was found in the repository."})
    missing_files.extend(item for info in module_infos for item in info.get("missing_files", []))
    missing_files.extend(detect_missing_gradle_modules(settings_files, root))

    module_details = []
    all_dependencies = []
    resolution_notes = []
    maven_update_records = []
    for info in module_infos:
        if info.get("is_aggregator") and not info["direct_dependencies"]:
            tree, resolution_error = [], None
        else:
            tree, resolution_error = resolve_dependency_tree(info, root)
        if info.get("build_system") == "Maven" and not (info.get("is_aggregator") and not info["direct_dependencies"]):
            update_data, update_error = resolve_maven_updates(info, root)
            if update_data:
                maven_update_records.append(update_data)
            elif update_error:
                resolution_notes.append({"module": rel(info["path"].parent, root), "message": update_error})
        if resolution_error:
            resolution_notes.append({"module": rel(info["path"].parent, root), "message": resolution_error})
            issues.append({
                "title": "Dependency versions may be incomplete",
                "severity": "MEDIUM",
                "path": rel(info["path"], root),
                "message": resolution_error + ". Declared versions are retained; inherited/transitive versions may be missing.",
            })
        merged = merge_dependencies(info, tree)
        for dep in merged:
            if not dep.get("version"):
                issues.append({
                    "title": "Dependency version unresolved",
                    "severity": "MEDIUM",
                    "path": dep.get("source"),
                    "message": f"The version for {dep['group_id']}:{dep['artifact_id']} could not be resolved from the build files or dependency tree.",
                })
        if info.get("error"):
            issues.append({"title": "Invalid build file", "severity": "HIGH", "path": rel(info["path"], root), "message": info["error"][:220]})
        all_dependencies.extend(merged)
        module_details.append({
            "name": info["path"].parent.name,
            "path": rel(info["path"].parent, root),
            "build_file": rel(info["path"], root),
            "build_system": info["build_system"],
            "java_version": choose_java(info),
        })

    unique_dependencies = {}
    for dep in all_dependencies:
        key = (dep.get("module"), dep["group_id"], dep["artifact_id"], dep.get("version"))
        if key not in unique_dependencies or dep.get("direct"):
            unique_dependencies[key] = dep
    all_dependencies = list(unique_dependencies.values())

    metadata_coordinates = {(d["group_id"], d["artifact_id"]) for d in all_dependencies if d.get("version")}
    metadata_coordinates.add(("org.springframework.boot", "spring-boot-starter-parent"))
    metadata_cache = fetch_metadata_for(metadata_coordinates)

    # Merge Maven's repository-aware update candidates when direct Python HTTP lookups fail.
    for update_data in maven_update_records:
        for coordinate, candidate_data in update_data.items():
            existing = metadata_cache.get(coordinate, {"versions": [], "status": "UNAVAILABLE"})
            combined = set(existing.get("versions", []) or [])
            combined.update(candidate_data.get("versions", []) or [])
            combined = sorted((v for v in combined if version_tuple(v)), key=version_sort_key)
            if combined:
                metadata_cache[coordinate] = {
                    "status": "FOUND",
                    "versions": combined,
                    "latest_version": combined[-1],
                    "source": candidate_data.get("source") or existing.get("source"),
                }

    dependencies = []
    for dep in all_dependencies:
        item = dict(dep)
        advice = version_advice(item.get("version"), metadata_cache.get((item["group_id"], item["artifact_id"]), {"versions": []}))
        item.update(advice)
        if not item.get("version"):
            item["version_status"] = "VERSION_UNRESOLVED"
        dependencies.append(item)

    frameworks = []
    for info in module_infos:
        module_key = rel(info["path"].parent, root)
        module_deps = [d for d in dependencies if d.get("module") == module_key]
        frameworks.extend(identify_frameworks(module_deps, info, metadata_cache, root))

    framework_unique = {}
    for item in frameworks:
        framework_unique[(item["name"], item.get("module"), item["version"])] = item
    frameworks = sorted(framework_unique.values(), key=lambda x: (x["name"], str(x.get("module")), str(x.get("version"))))

    java_versions = [m["java_version"] for m in module_details if m["java_version"] not in {"Not detected", "Multiple"}]
    current_java = "Multiple" if any(m["java_version"] == "Multiple" for m in module_details) or len(set(java_versions)) > 1 else (java_versions[0] if java_versions else "Not detected")
    build_system = systems[0] if len(systems) == 1 else ("Multiple" if systems else "Unknown")

    minimum_java = int(MINIMUM_JAVA_VERSION)
    for module in module_details:
        current = module["java_version"]
        detected = java_major(current) if current not in {"Not detected", "Multiple"} else None
        if detected and int(detected) < minimum_java:
            issues.append({
                "title": "Java version below JDK 8 baseline",
                "severity": "HIGH",
                "status": "NEEDS_CHANGES",
                "path": module["build_file"],
                "current_version": detected,
                "recommended_version": MINIMUM_JAVA_VERSION,
                "message": f"Java {detected} is below the minimum JDK {MINIMUM_JAVA_VERSION} baseline. Upgrade the compiler/source target and verify APIs and dependencies.",
            })

    java_by_module = {m["path"]: java_major(m["java_version"]) for m in module_details}
    for framework in frameworks:
        if framework["name"] != "Spring Boot" or not version_tuple(framework.get("version")):
            continue
        java_version = java_by_module.get(framework.get("module"))
        if version_tuple(framework["version"])[0] >= 3 and java_version and int(java_version) < 17:
            framework["version_status"] = "NEEDS_CHANGES"
            framework["change_reason"] = f"Spring Boot {framework['version']} requires Java 17 or newer, but this module targets Java {java_version}."
            issues.append({
                "title": "Java version incompatible with Spring Boot",
                "severity": "HIGH",
                "status": "NEEDS_CHANGES",
                "path": framework.get("source"),
                "message": framework["change_reason"],
            })

    # Only show frameworks with a confirmed update or a known compatibility change.
    frameworks = [
        item for item in frameworks
        if item.get("version_status") in {"NEEDS_UPDATE", "MAJOR_UPDATE_AVAILABLE", "NEEDS_CHANGES"}
    ]

    issues.extend(scan_deprecated_apis(root))
    for item in missing_files:
        issues.append({"title": "Missing referenced file or project", "severity": "HIGH", "path": item["path"], "message": item["reason"]})

    recommendations = []
    recommendation_keys = set()
    for item in dependencies:
        current, recommended, status = item.get("version"), item.get("recommended_version"), item.get("version_status")
        if status in {"NEEDS_UPDATE", "MAJOR_UPDATE_AVAILABLE"} and current and recommended and recommended != current:
            name = f"{item['group_id']}:{item['artifact_id']}"
            key = (item.get("module"), name, current, recommended)
            if key not in recommendation_keys:
                recommendation_keys.add(key)
                reason = "A newer stable release is available in the current major line." if status == "NEEDS_UPDATE" else "A newer major version is available; review release notes and Java compatibility before upgrading."
                recommendations.append({"component": name, "module": item.get("module"), "current_version": current, "recommended_version": recommended, "reason": reason})

    for item in frameworks:
        current, recommended = item.get("version"), item.get("recommended_version")
        if current and recommended and recommended != current and item.get("version_status") in {"NEEDS_UPDATE", "MAJOR_UPDATE_AVAILABLE"}:
            key = (item.get("module"), item["name"], current, recommended)
            if key not in recommendation_keys:
                recommendation_keys.add(key)
                recommendations.append({
                    "component": item["name"], "module": item.get("module"),
                    "current_version": current, "recommended_version": recommended,
                    "reason": "Framework update identified from Maven Central metadata; verify Java requirements and release notes.",
                })

    for module in module_details:
        current = module.get("java_version")
        detected = java_major(current) if current not in {"Not detected", "Multiple"} else None
        if detected and int(detected) < int(MINIMUM_JAVA_VERSION):
            recommendations.append({
                "component": "Java",
                "module": module.get("path"),
                "current_version": detected,
                "recommended_version": MINIMUM_JAVA_VERSION,
                "version_status": "NEEDS_CHANGES",
                "reason": f"Java versions below JDK {MINIMUM_JAVA_VERSION} should be upgraded and compatibility changes reviewed.",
            })

    severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    issues.sort(key=lambda x: (severity_rank.get(x.get("severity"), 3), x.get("title", ""), x.get("path") or ""))
    recommendations.sort(key=lambda x: (str(x.get("module")), x["component"]))

    return {
        "project_name": root.name,
        "build_system": build_system,
        "current_java": current_java,
        "minimum_java": MINIMUM_JAVA_VERSION,
        "modules": module_details,
        "frameworks": frameworks,
        "dependencies": [
            {
                "name": f"{d['group_id']}:{d['artifact_id']}",
                "version": d.get("version") or "Not resolved",
                "scope": d.get("scope"),
                "type": "direct" if d.get("direct") else "transitive",
                "version_source": d.get("version_source"),
                "module": d.get("module"),
                "latest_version": d.get("latest_version") or "Unavailable",
                "recommended_version": d.get("recommended_version") or "Unavailable",
                "version_status": d.get("version_status") or "CHECK_UNAVAILABLE",
            }
            for d in sorted(
                [d for d in dependencies if d.get("version_status") in {"NEEDS_UPDATE", "MAJOR_UPDATE_AVAILABLE"}],
                key=lambda x: (str(x.get("module")), x["group_id"], x["artifact_id"], str(x.get("version")))
            )
        ],
        "missing_files": missing_files,
        "issues": issues,
        "recommendations": recommendations,
        "analysis_notes": [
            "No application runtime checks or build/test validation are performed.",
            "Latest/recommended versions use Maven Central metadata with Maven Versions Plugin fallback; a component is listed for updating only when a newer release is established.",
        ] + ([f"Dependency resolution was incomplete for {len(resolution_notes)} module(s); see Issues."] if resolution_notes else []),
    }


def main():
    if len(sys.argv) != 2:
        print('Usage: python auditor.py "PATH_TO_JAVA_PROJECT"')
        raise SystemExit(2)
    try:
        report = build_report(Path(sys.argv[1]).expanduser())
    except Exception as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    output = Path.cwd() / "output.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Created {output}")


if __name__ == "__main__":
    main()
