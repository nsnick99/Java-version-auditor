from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

BUILD = {
    "pom.xml": "Maven",
    "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle",
    "settings.gradle": "Gradle",
    "settings.gradle.kts": "Gradle",
    "build.xml": "Ant",
}
SKIP = {
    ".git", "target", "build", "out", "bin", "node_modules", ".gradle",
    "__pycache__", ".venv", "venv", ".idea",
}
USER_AGENT = "Java-Version-Auditor/0.2 (dependency-version-inventory)"
HTTP_TIMEOUT = 8
MAX_WORKERS = 8

# These are explicit maintenance/deprecation advisories, not generic guesses.
# Sources are maintainer documentation and should be reviewed if project status changes.
KNOWN_ADVISORIES = [
    {
        "matches": [{"group": "com.netflix.hystrix"}],
        "status": "MAINTENANCE_MODE",
        "name": "Netflix Hystrix",
        "reason": "The upstream project states it is no longer in active development and is in maintenance mode.",
        "alternative": "Resilience4j / Spring Cloud CircuitBreaker with Resilience4j; not a drop-in replacement.",
        "source_url": "https://github.com/Netflix/Hystrix",
    },
    {
        "matches": [
            {"group": "com.netflix.ribbon"},
            {"group": "org.springframework.cloud", "artifact_prefix": "spring-cloud-starter-netflix-ribbon"},
            {"group": "org.springframework.cloud", "artifact_prefix": "spring-cloud-netflix-ribbon"},
        ],
        "status": "MAINTENANCE_MODE",
        "name": "Netflix Ribbon",
        "reason": "Spring Cloud documentation identifies Ribbon as being in maintenance mode and recommends Spring Cloud LoadBalancer.",
        "alternative": "Spring Cloud LoadBalancer; review configuration and integration changes.",
        "source_url": "https://docs.spring.io/spring-cloud-netflix/docs/2.2.8.RELEASE/reference/html/",
    },
]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_name(element) -> str:
    return element.tag.split("}")[-1] if element is not None else ""


def child(element, name):
    if element is None:
        return None
    return next((item for item in list(element) if local_name(item) == name), None)


def children(element, name):
    if element is None:
        return []
    return [item for item in list(element) if local_name(item) == name]


def text_child(element, name):
    item = child(element, name)
    if item is None or item.text is None:
        return None
    value = item.text.strip()
    return value or None


def clean(value):
    if value is None:
        return None
    value = str(value).strip().strip("'\"")
    if not value or value.startswith("$" + "{"):
        return None
    match = re.fullmatch(r"(?:JavaVersion\.)?VERSION_(?:1_)?(\d+)", value, re.I)
    return match.group(1) if match else value


def java_major(value):
    value = clean(value)
    if not value:
        return None
    if value.startswith("1.") and len(value.split(".")) > 1:
        return value.split(".")[1]
    match = re.match(r"\d+", value)
    return match.group(0) if match else value


def version_numbers(value):
    if not value:
        return []
    match = re.match(r"^\s*[vV]?(\d+(?:\.\d+)*)", str(value))
    if not match:
        return []
    try:
        return [int(part) for part in match.group(1).split(".")]
    except ValueError:
        return []


def compare_versions(left, right):
    """Return -1, 0, 1 for a practical numeric comparison of release versions."""
    left_numbers = version_numbers(left)
    right_numbers = version_numbers(right)
    if not left_numbers or not right_numbers:
        return 0
    width = max(len(left_numbers), len(right_numbers))
    left_numbers += [0] * (width - len(left_numbers))
    right_numbers += [0] * (width - len(right_numbers))
    if left_numbers < right_numbers:
        return -1
    if left_numbers > right_numbers:
        return 1

    # For the same numeric base, a pre-release is older than a final release.
    left_tail = str(left).strip().lower()[len(re.match(r"^\s*[vV]?\d+(?:\.\d+)*", str(left)).group(0).strip()):]
    right_tail = str(right).strip().lower()[len(re.match(r"^\s*[vV]?\d+(?:\.\d+)*", str(right)).group(0).strip()):]
    prerelease = re.compile(r"(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea|cr)\b", re.I)
    left_pre = bool(prerelease.search(left_tail))
    right_pre = bool(prerelease.search(right_tail))
    if left_pre != right_pre:
        return -1 if left_pre else 1
    return 0


def resolve_value(value, properties, max_passes=12):
    if value is None:
        return None
    result = str(value).strip()
    pattern = re.compile(r"\$[{]([^}]+)[}]")
    for _ in range(max_passes):
        matches = list(pattern.finditer(result))
        if not matches:
            break
        changed = False
        for match in reversed(matches):
            key = match.group(1).strip()
            replacement = properties.get(key)
            if replacement is None or str(replacement) == match.group(0):
                continue
            result = result[:match.start()] + str(replacement) + result[match.end():]
            changed = True
        if not changed:
            break
    if pattern.search(result):
        return None
    return result.strip() or None


def maven_repo_pom(group_id, artifact_id, version):
    if not all((group_id, artifact_id, version)):
        return None
    group_path = str(group_id).replace(".", os.sep)
    candidate = Path.home() / ".m2" / "repository" / group_path / str(artifact_id) / str(version) / (str(artifact_id) + "-" + str(version) + ".pom")
    return candidate if candidate.is_file() else None


def load_maven_model(pom_path, seen=None, depth=0):
    """Read local parent/BOM metadata when available; never invokes Maven."""
    pom_path = Path(pom_path).resolve()
    seen = set() if seen is None else seen
    key = str(pom_path)
    if key in seen or depth > 8 or not pom_path.is_file():
        return {"properties": {}, "dependency_management": {}, "plugin_management": {}, "coordinates": {}}
    seen.add(key)

    try:
        root = ET.parse(pom_path).getroot()
    except (ET.ParseError, OSError):
        return {"properties": {}, "dependency_management": {}, "plugin_management": {}, "coordinates": {}}

    raw_properties = {}
    properties_node = child(root, "properties")
    if properties_node is not None:
        for item in list(properties_node):
            if item.text:
                raw_properties[local_name(item)] = item.text.strip()

    parent_node = child(root, "parent")
    parent_group = text_child(parent_node, "groupId")
    parent_artifact = text_child(parent_node, "artifactId")
    parent_version = text_child(parent_node, "version")

    preliminary = dict(raw_properties)
    preliminary.update({
        "project.groupId": text_child(root, "groupId") or parent_group or "",
        "pom.groupId": text_child(root, "groupId") or parent_group or "",
        "project.artifactId": text_child(root, "artifactId") or "",
        "pom.artifactId": text_child(root, "artifactId") or "",
        "project.version": text_child(root, "version") or parent_version or "",
        "pom.version": text_child(root, "version") or parent_version or "",
        "project.parent.groupId": parent_group or "",
        "project.parent.artifactId": parent_artifact or "",
        "project.parent.version": parent_version or "",
        "parent.version": parent_version or "",
    })

    parent_model = {"properties": {}, "dependency_management": {}, "plugin_management": {}, "coordinates": {}}
    if parent_node is not None:
        relative_node = child(parent_node, "relativePath")
        relative_value = "../pom.xml" if relative_node is None else (relative_node.text or "").strip()
        possible_parent = None
        if relative_value:
            candidate = pom_path.parent / relative_value
            if candidate.is_dir():
                candidate = candidate / "pom.xml"
            if candidate.is_file():
                possible_parent = candidate
        if possible_parent is None:
            pg = resolve_value(parent_group, preliminary)
            pa = resolve_value(parent_artifact, preliminary)
            pv = resolve_value(parent_version, preliminary)
            possible_parent = maven_repo_pom(pg, pa, pv)
        if possible_parent is not None:
            parent_model = load_maven_model(possible_parent, seen, depth + 1)

    properties = dict(parent_model.get("properties", {}))
    properties.update(raw_properties)
    coordinates = {
        "groupId": resolve_value(text_child(root, "groupId"), properties)
                   or parent_model.get("coordinates", {}).get("groupId")
                   or resolve_value(parent_group, properties),
        "artifactId": resolve_value(text_child(root, "artifactId"), properties),
        "version": resolve_value(text_child(root, "version"), properties)
                   or resolve_value(parent_version, properties),
    }
    properties.update({
        "project.groupId": coordinates.get("groupId") or "",
        "pom.groupId": coordinates.get("groupId") or "",
        "groupId": coordinates.get("groupId") or "",
        "project.artifactId": coordinates.get("artifactId") or "",
        "pom.artifactId": coordinates.get("artifactId") or "",
        "artifactId": coordinates.get("artifactId") or "",
        "project.version": coordinates.get("version") or "",
        "pom.version": coordinates.get("version") or "",
        "version": coordinates.get("version") or "",
        "project.parent.groupId": resolve_value(parent_group, properties) or "",
        "project.parent.artifactId": resolve_value(parent_artifact, properties) or "",
        "project.parent.version": resolve_value(parent_version, properties) or "",
        "parent.version": resolve_value(parent_version, properties) or "",
    })
    for _ in range(6):
        updated = {name: resolve_value(value, properties) or value for name, value in properties.items()}
        if updated == properties:
            break
        properties.update(updated)

    dependency_management = dict(parent_model.get("dependency_management", {}))
    management_node = child(child(root, "dependencyManagement"), "dependencies")
    managed_entries = children(management_node, "dependency")

    # Imported BOMs first; explicit entries in this POM take precedence afterwards.
    for dep in managed_entries:
        if (text_child(dep, "type") or "jar") != "pom" or (text_child(dep, "scope") or "") != "import":
            continue
        group = resolve_value(text_child(dep, "groupId"), properties)
        artifact = resolve_value(text_child(dep, "artifactId"), properties)
        version = resolve_value(text_child(dep, "version"), properties)
        bom_path = maven_repo_pom(group, artifact, version)
        if bom_path:
            bom_model = load_maven_model(bom_path, seen.copy(), depth + 1)
            for bom_key, bom_value in bom_model.get("dependency_management", {}).items():
                dependency_management.setdefault(bom_key, bom_value)

    for dep in managed_entries:
        if (text_child(dep, "type") or "jar") == "pom" and (text_child(dep, "scope") or "") == "import":
            continue
        group = resolve_value(text_child(dep, "groupId"), properties)
        artifact = resolve_value(text_child(dep, "artifactId"), properties)
        version = resolve_value(text_child(dep, "version"), properties)
        if group and artifact and version:
            dep_key = group + ":" + artifact + ":" + (text_child(dep, "type") or "jar")
            dependency_management[dep_key] = {
                "version": version,
                "source": str(pom_path.name) + " > dependencyManagement",
            }
            dependency_management[group + ":" + artifact] = dependency_management[dep_key]

    plugin_management = dict(parent_model.get("plugin_management", {}))
    plugin_management_node = child(child(child(root, "build"), "pluginManagement"), "plugins")
    for plugin in children(plugin_management_node, "plugin"):
        group = resolve_value(text_child(plugin, "groupId") or "org.apache.maven.plugins", properties)
        artifact = resolve_value(text_child(plugin, "artifactId"), properties)
        version = resolve_value(text_child(plugin, "version"), properties)
        if group and artifact and version:
            plugin_management[group + ":" + artifact] = {
                "version": version,
                "source": str(pom_path.name) + " > pluginManagement",
            }

    return {
        "properties": properties,
        "dependency_management": dependency_management,
        "plugin_management": plugin_management,
        "coordinates": coordinates,
    }


def maven(pom_path, project_root):
    java_versions = []
    boot_versions = []
    components = []
    try:
        root = ET.parse(pom_path).getroot()
    except (ET.ParseError, OSError):
        return java_versions, boot_versions, components

    model = load_maven_model(pom_path)
    props = model["properties"]
    relative_source = str(Path(pom_path).resolve().relative_to(Path(project_root).resolve()))
    properties_node = child(root, "properties")
    if properties_node is not None:
        for item in list(properties_node):
            name = local_name(item)
            if not item.text:
                continue
            val = resolve_value(item.text.strip(), props) or item.text.strip()
            if name in {"java.version", "maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "jdk.version"}:
                java_versions.append((java_major(val), relative_source + " > properties." + name))
            if "spring" in name.lower() and "boot" in name.lower() and "version" in name.lower():
                boot_versions.append((clean(val), relative_source + " > properties." + name))

    parent = child(root, "parent")
    parent_group = resolve_value(text_child(parent, "groupId"), props)
    parent_artifact = resolve_value(text_child(parent, "artifactId"), props)
    parent_version = resolve_value(text_child(parent, "version"), props)
    if parent_group == "org.springframework.boot" and parent_artifact == "spring-boot-starter-parent":
        boot_versions.append((clean(parent_version), relative_source + " > parent.version"))

    # A project may import the Spring Boot BOM without inheriting its parent.
    for dep in children(child(child(root, "dependencyManagement"), "dependencies"), "dependency"):
        if resolve_value(text_child(dep, "groupId"), props) == "org.springframework.boot" and resolve_value(text_child(dep, "artifactId"), props) == "spring-boot-dependencies":
            boot_versions.append((clean(resolve_value(text_child(dep, "version"), props)), relative_source + " > dependencyManagement.spring-boot-dependencies"))

    def version_for(dep, group, artifact, dep_type="jar"):
        declared = resolve_value(text_child(dep, "version"), props)
        if declared:
            return declared, "declared"
        managed = model["dependency_management"].get(group + ":" + artifact + ":" + dep_type) or model["dependency_management"].get(group + ":" + artifact)
        if managed:
            return managed["version"], "dependency_management"
        return None, "unresolved_or_remote_managed"

    # Only direct top-level dependencies are inventory items; dependencyManagement
    # definitions are version rules, not actual dependencies.
    for dep in children(child(root, "dependencies"), "dependency"):
        group = resolve_value(text_child(dep, "groupId"), props)
        artifact = resolve_value(text_child(dep, "artifactId"), props)
        if not group or not artifact:
            continue
        dep_type = resolve_value(text_child(dep, "type"), props) or "jar"
        version, source_kind = version_for(dep, group, artifact, dep_type)
        scope = resolve_value(text_child(dep, "scope"), props) or "compile"
        components.append({
            "group_id": group,
            "artifact_id": artifact,
            "version": clean(version),
            "declared_version": clean(resolve_value(text_child(dep, "version"), props)),
            "version_source": source_kind,
            "scope": scope,
            "optional": (text_child(dep, "optional") or "").lower() == "true",
            "direct": True,
            "component_type": "dependency",
            "source": relative_source,
        })

    # Inventory explicit build plugins too; versions may be inherited through pluginManagement.
    build_plugins = child(child(root, "build"), "plugins")
    for plugin in children(build_plugins, "plugin"):
        group = resolve_value(text_child(plugin, "groupId"), props) or "org.apache.maven.plugins"
        artifact = resolve_value(text_child(plugin, "artifactId"), props)
        if not artifact:
            continue
        declared = resolve_value(text_child(plugin, "version"), props)
        managed = model["plugin_management"].get(group + ":" + artifact)
        version = declared or (managed or {}).get("version")
        components.append({
            "group_id": group,
            "artifact_id": artifact,
            "version": clean(version),
            "declared_version": clean(declared),
            "version_source": "declared" if declared else ("plugin_management" if managed else "unresolved_or_parent_managed"),
            "scope": "build",
            "optional": False,
            "direct": True,
            "component_type": "build_plugin",
            "source": relative_source,
        })

    boot_plugin = next((
        plugin for plugin in children(build_plugins, "plugin")
        if (resolve_value(text_child(plugin, "groupId"), props) or "org.apache.maven.plugins") == "org.springframework.boot"
        and resolve_value(text_child(plugin, "artifactId"), props) == "spring-boot-maven-plugin"
    ), None)
    if boot_plugin is not None:
        version = resolve_value(text_child(boot_plugin, "version"), props)
        if not version:
            version = (model["plugin_management"].get("org.springframework.boot:spring-boot-maven-plugin") or {}).get("version")
        if version:
            boot_versions.append((clean(version), relative_source + " > spring-boot-maven-plugin.version"))

    return java_versions, boot_versions, components


def parse_catalog(catalog_path):
    """Read the common Gradle version-catalog structure with a tomllib fallback."""
    if not catalog_path.is_file():
        return {}, {}
    versions = {}
    libraries = {}
    try:
        import tomllib
        with catalog_path.open("rb") as handle:
            data = tomllib.load(handle)
        versions.update({str(k): str(v) for k, v in data.get("versions", {}).items() if isinstance(v, (str, int, float))})
        for alias, spec in data.get("libraries", {}).items():
            if isinstance(spec, str):
                module, version = spec, None
            else:
                module = spec.get("module")
                if not module and spec.get("group") and spec.get("name"):
                    module = str(spec["group"]) + ":" + str(spec["name"])
                version_spec = spec.get("version")
                if isinstance(version_spec, dict):
                    version = versions.get(str(version_spec.get("ref"))) if version_spec.get("ref") else None
                    version = version or version_spec.get("require") or version_spec.get("strictly")
                else:
                    version = version_spec
                version = version or (versions.get(str(spec.get("version.ref"))) if spec.get("version.ref") else None)
            if module and ":" in str(module):
                group, artifact = str(module).split(":", 1)
                libraries[str(alias)] = {"group_id": group, "artifact_id": artifact, "version": clean(version)}
        return versions, libraries
    except (ImportError, OSError, ValueError):
        pass

    text = catalog_path.read_text(encoding="utf-8", errors="replace")
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        section_match = re.match(r"\[([^\]]+)\]", stripped)
        if section_match:
            section = section_match.group(1)
            continue
        if section == "versions":
            match = re.match(r'([A-Za-z0-9_.-]+)\s*=\s*["\']([^"\']+)["\']', stripped)
            if match:
                versions[match.group(1)] = match.group(2)
        elif section == "libraries":
            match = re.match(r'([A-Za-z0-9_.-]+)\s*=\s*\{.*module\s*=\s*["\']([^"\']+)["\'].*\}', stripped)
            if match:
                alias, module = match.groups()
                version = None
                ref = re.search(r'version\.ref\s*=\s*["\']([^"\']+)["\']', stripped)
                inline = re.search(r'version\s*=\s*["\']([^"\']+)["\']', stripped)
                if ref:
                    version = versions.get(ref.group(1))
                elif inline:
                    version = inline.group(1)
                if ":" in module:
                    group, artifact = module.split(":", 1)
                    libraries[alias] = {"group_id": group, "artifact_id": artifact, "version": clean(version)}
    return versions, libraries


def find_catalog(build_file, root):
    candidates = [
        build_file.parent / "gradle" / "libs.versions.toml",
        root / "gradle" / "libs.versions.toml",
    ]
    for parent in build_file.parents:
        candidates.append(parent / "gradle" / "libs.versions.toml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def gradle(build_file, root):
    try:
        source = build_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], [], []

    java_versions = []
    boot_versions = []
    components = []
    rel = str(build_file.resolve().relative_to(Path(root).resolve()))
    catalog_path = find_catalog(build_file, Path(root))
    catalog_versions, catalog_libraries = parse_catalog(catalog_path) if catalog_path else ({}, {})

    for pattern in [
        r"(?:sourceCompatibility|targetCompatibility)\s*=\s*['\"](1\.\d+|\d+(?:\.\d+)*)",
        r"JavaLanguageVersion\.of\(\s*['\"]?(\d+)['\"]?\s*\)",
        r"VERSION_(?:1_)?(\d+)\b",
    ]:
        java_versions.extend((java_major(m.group(1)), rel + " > Java configuration") for m in re.finditer(pattern, source, re.I))

    boot_patterns = [
        r"org\.springframework\.boot[\"']?\s*\)?\s*version\s*[\"']([^\"']+)",
        r"springBootVersion\s*=\s*[\"']([^\"']+)",
        r"id\s*\(?\s*[\"']org\.springframework\.boot[\"']\s*\)?\s*version\s*[\"']([^\"']+)",
        r"id\s+['\"]org\.springframework\.boot['\"]\s+version\s+['\"]([^'\"]+)",
        r"springBoot\s*=\s*[\"']([^\"']+)",
    ]
    for pattern in boot_patterns:
        boot_versions.extend((clean(m.group(1)), rel + " > Spring Boot configuration") for m in re.finditer(pattern, source, re.I))

    # Dependency notation such as implementation("org.example:library:1.2.3").
    dep_pattern = re.compile(
        r"\b(implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|"
        r"annotationProcessor|kapt|testCompileOnly|testApi|classpath|compile|runtime|testCompile)"
        r"\s*(?:\(\s*)?['\"]([^'\" \n]+)['\"]\s*\)?",
        re.I,
    )
    seen = set()
    for match in dep_pattern.finditer(source):
        scope, notation = match.group(1), match.group(2)
        parts = notation.split(":")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        group, artifact = parts[0], parts[1]
        version = parts[2] if len(parts) >= 3 else None
        key = (group, artifact, version, scope.lower())
        if key in seen:
            continue
        seen.add(key)
        components.append({
            "group_id": group,
            "artifact_id": artifact,
            "version": clean(version),
            "declared_version": clean(version),
            "version_source": "declared" if version else "managed_or_platform_or_unknown",
            "scope": scope,
            "optional": False,
            "direct": True,
            "component_type": "dependency",
            "source": rel,
        })

    # Version catalog aliases: libs.some.library, libs.someLibrary, etc.
    if catalog_libraries:
        alias_patterns = [
            r"\b(?:implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor|kapt)"
            r"\s*(?:\(\s*)?(libs(?:\.[A-Za-z0-9_-]+)+)",
        ]
        for pattern in alias_patterns:
            for match in re.finditer(pattern, source):
                alias_expr = match.group(1)[len("libs."):]
                aliases = {alias_expr, alias_expr.replace(".", "-"), alias_expr.replace("-", ".")}
                spec = next((catalog_libraries[a] for a in aliases if a in catalog_libraries), None)
                if not spec:
                    continue
                group, artifact, version = spec["group_id"], spec["artifact_id"], spec.get("version")
                key = (group, artifact, version, "catalog")
                if key in seen:
                    continue
                seen.add(key)
                components.append({
                    "group_id": group,
                    "artifact_id": artifact,
                    "version": clean(version),
                    "declared_version": clean(version),
                    "version_source": "version_catalog" if version else "version_catalog_without_resolved_version",
                    "scope": "gradle_catalog",
                    "optional": False,
                    "direct": True,
                    "component_type": "dependency",
                    "source": str(catalog_path.resolve().relative_to(Path(root).resolve())),
                })

    # Gradle plugins have their own versions and should also be audited.
    plugin_pattern = re.compile(
        r"""(?:id\s*\(?\s*['"]([^'"]+)['"]\s*\)?\s*version\s*['"]([^'"]+)['"]|"""
        r"""id\s+['"]([^'"]+)['"]\s+version\s+['"]([^'"]+)['"])""",
        re.I,
    )
    for match in plugin_pattern.finditer(source):
        plugin_id = match.group(1) or match.group(3)
        version = match.group(2) or match.group(4)
        if plugin_id and version:
            components.append({
                "group_id": "gradle.plugin",
                "artifact_id": plugin_id,
                "version": clean(version),
                "declared_version": clean(version),
                "version_source": "declared",
                "scope": "build",
                "optional": False,
                "direct": True,
                "component_type": "build_plugin",
                "source": rel,
                "latest_lookup_supported": False,
            })

    # Gradle property references are common; resolve straightforward single-line assignments.
    property_values = {}
    for match in re.finditer(r"(?m)^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*=\s*['\"]([^'\"]+)['\"]\s*$", source):
        property_values[match.group(1)] = match.group(2)
    for component in components:
        # Static scanner keeps unresolved property references visible rather than guessing.
        if component.get("version") and component["version"].startswith("$"):
            prop_key = component["version"].lstrip("$").strip("{}")
            if prop_key in property_values:
                component["version"] = clean(property_values[prop_key])
                component["version_source"] = "gradle_property"

    return java_versions, boot_versions, components


def ant(build_file, root):
    java_versions = []
    boot_versions = []
    components = []
    props = build_file.with_name("build.properties")
    rel_props = str(props.resolve().relative_to(Path(root).resolve()))
    if props.is_file():
        for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line or line.lstrip().startswith(("#", "!")):
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key in {"java.version", "javaVersion", "jdk.version"}:
                java_versions.append((java_major(value), rel_props + " > " + key))
            if key in {"spring-boot.version", "spring.boot.version", "springBootVersion"}:
                boot_versions.append((clean(value), rel_props + " > " + key))

    # For Ant projects, JAR filenames are only a heuristic; keep that evidence explicit.
    for jar in build_file.parent.rglob("*.jar"):
        if any(part in SKIP for part in jar.parts):
            continue
        match = re.match(r"(.+?)-((?:\d+)\.)*\d+(?:[-.][A-Za-z0-9_.-]+)?\.jar$", jar.name)
        if match:
            artifact = jar.stem
            version_match = re.search(r"-(\d+(?:\.\d+)+(?:[-.][A-Za-z0-9_.-]+)?)$", artifact)
            if version_match:
                version = version_match.group(1)
                artifact_name = artifact[:version_match.start()]
                components.append({
                    "group_id": "unknown",
                    "artifact_id": artifact_name,
                    "version": clean(version),
                    "declared_version": clean(version),
                    "version_source": "jar_filename_heuristic",
                    "scope": "ant_classpath",
                    "optional": False,
                    "direct": False,
                    "component_type": "jar_file",
                    "source": str(jar.resolve().relative_to(Path(root).resolve())),
                    "latest_lookup_supported": False,
                })
    return java_versions, boot_versions, components


def finding(items):
    items = [item for item in items if item[0]]
    versions = sorted({item[0] for item in items})
    if not versions:
        return {"status": "NOT_DETECTED", "version": None, "evidence": []}
    evidence = [{"version": version, "source": source} for version, source in items]
    if len(versions) > 1:
        return {"status": "CONFLICT", "version": None, "candidates": versions, "evidence": evidence}
    return {"status": "DETECTED", "version": versions[0], "evidence": evidence}


def resolve_maven_tree(folder, timeout=120):
    """Resolve Maven's dependency tree only when the user explicitly requests --resolve."""
    if not (folder / "pom.xml").is_file():
        return {"status": "NOT_APPLICABLE", "components": [], "message": "No pom.xml"}
    command = ["mvn", "dependency:tree", "-DoutputType=text", "-Dstyle.color=never"]
    try:
        proc = subprocess.run(
            command, cwd=str(folder), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return {"status": "TOOL_NOT_FOUND", "components": [], "message": "Maven executable was not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "components": [], "message": "Maven dependency resolution timed out."}
    if proc.returncode != 0:
        return {
            "status": "FAILED", "components": [],
            "message": (proc.stderr or proc.stdout or "Maven dependency:tree failed").strip()[-1200:],
        }

    resolved = []
    line_pattern = re.compile(
        r"([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([^:\s]+):([A-Za-z0-9_.-]+)"
    )
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        match = line_pattern.search(line)
        if not match:
            continue
        group, artifact, packaging, version, scope = match.groups()
        if not version or version.upper() in {"UNKNOWN", "UNRESOLVED"}:
            continue
        resolved.append({
            "group_id": group,
            "artifact_id": artifact,
            "version": clean(version),
            "declared_version": None,
            "version_source": "maven_dependency_tree",
            "scope": scope,
            "optional": False,
            "direct": False,
            "component_type": "dependency",
            "source": "mvn dependency:tree",
            "resolved_packaging": packaging,
        })
    return {"status": "RESOLVED", "components": resolved, "message": None}


def resolve_gradle_tree(folder, timeout=120):
    """Resolve Gradle runtime/test dependency graphs only when --resolve is supplied."""
    configurations = ["runtimeClasspath", "testRuntimeClasspath"]
    all_components = []
    errors = []
    resolved_any = False
    wrapper = folder / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if wrapper.is_file():
        base_command = [str(wrapper)] if os.name == "nt" else [str(wrapper)]
        if os.name != "nt" and not os.access(wrapper, os.X_OK):
            base_command = ["sh", str(wrapper)]
    else:
        base_command = ["gradle"]
    for configuration in configurations:
        command = base_command + ["dependencies", "--configuration", configuration, "--console=plain"]
        try:
            proc = subprocess.run(
                command, cwd=str(folder), capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except FileNotFoundError:
            return {"status": "TOOL_NOT_FOUND", "components": [], "message": "Gradle or its wrapper was not found."}
        except subprocess.TimeoutExpired:
            errors.append(configuration + ": timeout")
            continue
        if proc.returncode != 0:
            errors.append(configuration + ": " + (proc.stderr or proc.stdout or "resolution failed").strip()[-500:])
            continue
        resolved_any = True
        for line in (proc.stdout + "\n" + proc.stderr).splitlines():
            # Handles selected coordinates and conflict-resolution notation like 1.0 -> 1.2.
            match = re.search(r"([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([A-Za-z0-9_.+-]+)(?:\s+->\s+([A-Za-z0-9_.+-]+))?", line)
            if not match or not re.search(r"(?:\+---|\\---|---|\\---)", line):
                continue
            group, artifact, version, selected = match.groups()
            selected_version = selected or version
            if not version:
                continue
            all_components.append({
                "group_id": group,
                "artifact_id": artifact,
                "version": clean(selected_version),
                "declared_version": clean(version),
                "version_source": "gradle_dependency_report",
                "scope": configuration,
                "optional": False,
                "direct": False,
                "component_type": "dependency",
                "source": "gradle dependencies --configuration " + configuration,
            })
    if resolved_any:
        return {
            "status": "RESOLVED" if not errors else "PARTIAL",
            "components": all_components,
            "message": "; ".join(errors) if errors else None,
        }
    return {"status": "FAILED", "components": [], "message": "; ".join(errors) or "Gradle dependency resolution failed."}


def apply_resolution(module, resolved_result):
    module["dependency_resolution"] = {
        "status": resolved_result["status"],
        "message": resolved_result.get("message"),
        "resolved_component_count": len(resolved_result.get("components", [])),
    }
    if not resolved_result.get("components"):
        return

    declared = module.get("dependencies", [])
    by_coordinate = {}
    for item in declared:
        key = (item.get("group_id"), item.get("artifact_id"))
        by_coordinate.setdefault(key, []).append(item)

    merged = []
    emitted = set()
    for resolved in resolved_result["components"]:
        key = (resolved.get("group_id"), resolved.get("artifact_id"), resolved.get("version"), resolved.get("scope"))
        if key in emitted:
            continue
        emitted.add(key)
        declared_matches = by_coordinate.get((resolved.get("group_id"), resolved.get("artifact_id")), [])
        direct_match = next((item for item in declared_matches if item.get("component_type") == "dependency"), None)
        item = dict(resolved)
        item["direct"] = bool(direct_match)
        if direct_match:
            item["declared_version"] = direct_match.get("declared_version") or direct_match.get("version")
            item["component_type"] = direct_match.get("component_type", "dependency")
        merged.append(item)

    # Preserve direct items not emitted by the build-tool graph (for example inactive profiles).
    resolved_coordinates = {(item.get("group_id"), item.get("artifact_id")) for item in merged}
    for item in declared:
        key = (item.get("group_id"), item.get("artifact_id"))
        if key not in resolved_coordinates or item.get("component_type") == "build_plugin":
            merged.append(item)
    module["dependencies"] = merged


def http_json(url, timeout=HTTP_TIMEOUT):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def http_bytes(url, timeout=HTTP_TIMEOUT, accept="*/*"):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def maven_metadata_url(group_id, artifact_id):
    group_path = "/".join(urllib.parse.quote(part, safe="") for part in str(group_id).split("."))
    artifact = urllib.parse.quote(str(artifact_id), safe="")
    return "https://repo.maven.apache.org/maven2/" + group_path + "/" + artifact + "/maven-metadata.xml"


def is_prerelease(version):
    return bool(re.search(
        r"(?i)(?:^|[._-])(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea|cr)(?:[._-]?\d+)?(?:$|[._-])",
        str(version),
    ))


def latest_maven_version(group_id, artifact_id):
    if not group_id or not artifact_id:
        return {"status": "NOT_APPLICABLE", "version": None, "url": None}
    url = maven_metadata_url(group_id, artifact_id)
    try:
        raw = http_bytes(url, accept="application/xml,text/xml,*/*")
        root = ET.fromstring(raw)
    except urllib.error.HTTPError as error:
        return {"status": "NOT_FOUND" if error.code == 404 else "UNAVAILABLE", "version": None, "url": url, "message": "HTTP " + str(error.code)}
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError, ValueError) as error:
        return {"status": "UNAVAILABLE", "version": None, "url": url, "message": str(error)[:250]}

    def xml_text(name):
        node = child(root, name)
        return (node.text or "").strip() if node is not None and node.text else None

    versions_node = child(child(root, "versioning"), "versions")
    versions = [(item.text or "").strip() for item in children(versions_node, "version") if item.text]
    stable = [version for version in versions if not is_prerelease(version)]
    release = xml_text("release")
    if release and not is_prerelease(release):
        chosen = release
    elif stable:
        chosen = stable[0]
        for version in stable[1:]:
            if compare_versions(version, chosen) > 0:
                chosen = version
    else:
        latest = xml_text("latest")
        chosen = latest if latest and not is_prerelease(latest) else None

    if not chosen:
        return {"status": "NO_STABLE_RELEASE_FOUND", "version": None, "url": url}
    return {"status": "FOUND", "version": chosen, "url": url}


def lifecycle_product(slug):
    url = "https://endoflife.date/api/v1/products/" + urllib.parse.quote(slug, safe="")
    try:
        data = http_json(url)
        return {"status": "FOUND", "result": data.get("result", {}), "url": url, "generated_at": data.get("generated_at")}
    except urllib.error.HTTPError as error:
        return {"status": "NOT_CATALOGUED" if error.code == 404 else "UNAVAILABLE", "result": {}, "url": url, "message": "HTTP " + str(error.code)}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return {"status": "UNAVAILABLE", "result": {}, "url": url, "message": str(error)[:250]}


def lifecycle_cycle(product, version):
    numbers = version_numbers(version)
    if not numbers:
        return None
    if product == "java":
        return str(numbers[0])
    if product == "spring-boot":
        return ".".join(str(number) for number in numbers[:2]) if len(numbers) >= 2 else str(numbers[0])
    return ".".join(str(number) for number in numbers[:2]) if len(numbers) >= 2 else str(numbers[0])


def lifecycle_status(product_data, product, version):
    if product_data.get("status") != "FOUND":
        return {
            "status": product_data.get("status", "UNAVAILABLE"),
            "product": product,
            "version": version,
            "source_url": product_data.get("url"),
        }
    result = product_data.get("result", {})
    cycle = lifecycle_cycle(product, version)
    release = next((item for item in result.get("releases", []) if str(item.get("name")) == cycle), None)
    if not release:
        return {
            "status": "CYCLE_NOT_FOUND",
            "product": product,
            "cycle": cycle,
            "version": version,
            "source_url": result.get("links", {}).get("html", product_data.get("url")),
        }

    eol = release.get("isEol")
    eoes = release.get("isEoes")
    maintained = release.get("isMaintained")
    if eol is True and eoes is False and release.get("eoesFrom"):
        status = "OSS_SUPPORT_ENDED_EXTENDED_SUPPORT_AVAILABLE"
    elif eol is True:
        status = "EOL"
    elif maintained is True:
        status = "MAINTAINED"
    elif maintained is False:
        status = "NOT_MAINTAINED"
    else:
        status = "UNKNOWN"

    return {
        "status": status,
        "product": product,
        "cycle": cycle,
        "version": version,
        "eol_date": release.get("eolFrom"),
        "extended_support_end_date": release.get("eoesFrom"),
        "is_eol": eol,
        "is_maintained": maintained,
        "latest_in_cycle": release.get("latest", {}).get("name"),
        "latest_release_date": release.get("latest", {}).get("date"),
        "support_label": result.get("labels", {}).get("eol"),
        "extended_support_label": result.get("labels", {}).get("eoes"),
        "source_url": result.get("links", {}).get("html", product_data.get("url")),
        "data_generated_at": product_data.get("generated_at"),
    }


def lifecycle_slug_for_component(component):
    group = component.get("group_id") or ""
    artifact = component.get("artifact_id") or ""
    if group == "org.springframework.boot" or artifact.startswith("spring-boot-"):
        return "spring-boot"
    if group == "org.springframework" and artifact.startswith("spring-"):
        return "spring-framework"
    if group == "org.springframework.security" or artifact.startswith("spring-security-"):
        return "spring-security"
    if group in {"org.hibernate", "org.hibernate.orm"} and artifact == "hibernate-core":
        return "hibernate"
    if group == "org.apache.logging.log4j" or artifact.startswith("log4j-"):
        return "log4j"
    if group.startswith("org.apache.tomcat") or artifact.startswith("tomcat-"):
        return "tomcat"
    if group.startswith("org.apache.maven") or artifact.startswith("maven-"):
        return "maven"
    if group.startswith("org.gradle") or artifact.startswith("gradle-"):
        return "gradle"
    return None


def known_deprecation(component):
    group = component.get("group_id") or ""
    artifact = component.get("artifact_id") or ""
    for advisory in KNOWN_ADVISORIES:
        for rule in advisory["matches"]:
            if group == rule.get("group") or group.startswith(rule.get("group_prefix", "\0")):
                prefix = rule.get("artifact_prefix")
                exact = rule.get("artifact")
                if (not prefix and not exact) or (prefix and artifact.startswith(prefix)) or (exact and artifact == exact):
                    return {
                        "status": advisory["status"],
                        "name": advisory["name"],
                        "reason": advisory["reason"],
                        "recommended_alternative": advisory["alternative"],
                        "source_url": advisory["source_url"],
                        "kind": "maintainer_status_advisory",
                    }
    return {"status": "NOT_VERIFIED", "kind": "no_generic_deprecation_feed"}


def osv_query_batch(items):
    queries = []
    for component in items:
        group, artifact, version = component.get("group_id"), component.get("artifact_id"), component.get("version")
        if not all((group, artifact, version)) or component.get("latest_lookup_supported") is False:
            continue
        if str(group).lower() in {"unknown", "gradle.plugin"}:
            continue
        queries.append({
            "component_key": (group, artifact, version),
            "package": {"name": str(group) + ":" + str(artifact), "ecosystem": "Maven"},
            "version": str(version),
        })
    if not queries:
        return {"status": "NO_VERSIONED_COMPONENTS", "findings": {}, "checked_count": 0}

    results_by_key = {}
    errors = []
    batches = [queries[index:index + 100] for index in range(0, len(queries), 100)]
    for batch in batches:
        payload = {"queries": [{"package": item["package"], "version": item["version"]} for item in batch]}
        request = urllib.request.Request(
            "https://api.osv.dev/v1/querybatch",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            results = data.get("results", [])
            for index, item in enumerate(batch):
                vuln_ids = [vuln.get("id") for vuln in (results[index].get("vulns", []) if index < len(results) else []) if vuln.get("id")]
                results_by_key[item["component_key"]] = vuln_ids
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            errors.append(str(error)[:250])
            for item in batch:
                results_by_key[item["component_key"]] = None

    all_ids = sorted({vuln_id for vuln_ids in results_by_key.values() if vuln_ids for vuln_id in vuln_ids})
    details = {}
    def fetch_vuln(vuln_id):
        try:
            return vuln_id, http_json("https://api.osv.dev/v1/vulns/" + urllib.parse.quote(vuln_id, safe=""))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            return vuln_id, {"_error": str(error)[:200]}

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for vuln_id, detail in pool.map(fetch_vuln, all_ids):
            details[vuln_id] = detail

    findings = {}
    for item in queries:
        key = item["component_key"]
        vuln_ids = results_by_key.get(key)
        if vuln_ids is None:
            findings[key] = {"status": "UNAVAILABLE", "vulnerabilities": []}
            continue
        vulnerabilities = []
        for vuln_id in vuln_ids:
            vuln = details.get(vuln_id, {})
            if vuln.get("_error"):
                vulnerabilities.append({"id": vuln_id, "details_status": "UNAVAILABLE"})
                continue
            fixed_versions = set()
            for affected in vuln.get("affected", []):
                pkg = affected.get("package", {})
                if pkg.get("ecosystem") and pkg.get("ecosystem") != "Maven":
                    continue
                expected_name = item["package"]["name"]
                if pkg.get("name") and pkg.get("name") not in {expected_name, item["package"]["name"]}:
                    continue
                for version_range in affected.get("ranges", []):
                    for event in version_range.get("events", []):
                        if event.get("fixed"):
                            fixed_versions.add(event["fixed"])
                fixed_versions.update(affected.get("versions", [])[:0])

            severity = (
                vuln.get("database_specific", {}).get("severity")
                or vuln.get("ecosystem_specific", {}).get("severity")
                or (vuln.get("severity", [{}])[0].get("score") if vuln.get("severity") else None)
            )
            vulnerabilities.append({
                "id": vuln.get("id", vuln_id),
                "aliases": vuln.get("aliases", []),
                "summary": vuln.get("summary") or vuln.get("details", "")[:300],
                "severity": severity,
                "published": vuln.get("published"),
                "modified": vuln.get("modified"),
                "fixed_versions": sorted(fixed_versions, key=lambda value: version_numbers(value)),
                "references": [entry.get("url") for entry in vuln.get("references", []) if entry.get("url")][:10],
                "source_url": "https://osv.dev/vulnerability/" + urllib.parse.quote(vuln.get("id", vuln_id), safe=""),
            })
        findings[key] = {
            "status": "VULNERABLE" if vulnerabilities else "NO_MATCHING_ADVISORIES",
            "vulnerabilities": vulnerabilities,
        }
    return {
        "status": "PARTIAL" if errors else "COMPLETE",
        "findings": findings,
        "checked_count": len(queries),
        "unique_vulnerability_count": len(all_ids),
        "errors": errors,
        "source_url": "https://osv.dev/",
    }


def change_level(current, recommended):
    current_numbers = version_numbers(current)
    recommended_numbers = version_numbers(recommended)
    if not current_numbers or not recommended_numbers:
        return "REVIEW_VERSION_FORMAT"
    if current_numbers[0] != recommended_numbers[0]:
        return "MAJOR_VERSION_CHANGE_REVIEW_REQUIRED"
    if len(current_numbers) > 1 and len(recommended_numbers) > 1 and current_numbers[1] != recommended_numbers[1]:
        return "MINOR_VERSION_UPDATE"
    return "PATCH_OR_QUALIFIER_UPDATE"


def latest_java_lts():
    url = "https://api.adoptium.net/v3/info/available_releases"
    try:
        data = http_json(url)
        releases = data.get("available_lts_releases", [])
        recommended = max((int(value) for value in releases), default=None)
        return {
            "status": "FOUND" if recommended is not None else "NO_LTS_DATA",
            "recommended_version": str(recommended) if recommended is not None else None,
            "source_url": url,
            "basis": "Latest LTS release reported by Eclipse Adoptium; this is a reference, not a compatibility guarantee.",
        }
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return {"status": "UNAVAILABLE", "recommended_version": None, "source_url": url, "message": str(error)[:250]}


def enrich_report(report, online=True):
    components = []
    for module in report["modules"]:
        for component in module.get("dependencies", []):
            component["_module_path"] = module.get("path")
            components.append(component)

    # Deduplicate API requests without removing per-module inventory rows.
    unique_component_map = {}
    for component in components:
        group = component.get("group_id")
        artifact = component.get("artifact_id")
        version = component.get("version")
        key = (group, artifact, version)
        unique_component_map.setdefault(key, component)
    unique_components = list(unique_component_map.values())

    latest_map = {}
    lifecycle_product_map = {}
    osv_result = {"status": "SKIPPED", "findings": {}, "checked_count": 0}
    java_lts = {"status": "SKIPPED", "recommended_version": None}

    if online:
        latest_keys = sorted({
            (item.get("group_id"), item.get("artifact_id"))
            for item in unique_components
            if item.get("version") and item.get("group_id") and item.get("artifact_id")
            and item.get("latest_lookup_supported") is not False
            and item.get("group_id") not in {"unknown", "gradle.plugin"}
        })
        if any(module["spring_boot"].get("version") for module in report["modules"]):
            latest_keys = sorted(set(latest_keys) | {("org.springframework.boot", "spring-boot-starter-parent")})
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(latest_maven_version, group, artifact): (group, artifact)
                for group, artifact in latest_keys
            }
            for future in concurrent.futures.as_completed(futures):
                latest_map[futures[future]] = future.result()

        product_slugs = {
            slug for item in unique_components
            if item.get("version") and (slug := lifecycle_slug_for_component(item))
        }
        if any(module["java"].get("version") for module in report["modules"]):
            product_slugs.add("java")
        if any(module["spring_boot"].get("version") for module in report["modules"]):
            product_slugs.add("spring-boot")
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(lifecycle_product, slug): slug for slug in sorted(product_slugs)}
            for future in concurrent.futures.as_completed(futures):
                lifecycle_product_map[futures[future]] = future.result()
        osv_result = osv_query_batch(unique_components)
        java_lts = latest_java_lts()

    recommendations = []
    lifecycle_findings = []
    deprecation_findings = []
    vulnerability_findings = []

    def enrich_component(component):
        group = component.get("group_id")
        artifact = component.get("artifact_id")
        version = component.get("version")
        component_key = (group, artifact, version)
        result = {
            "status": "SKIPPED" if not online else "VERSION_NOT_RESOLVED",
            "latest_version": None,
            "update_available": None,
            "change_level": None,
            "latest_source_url": None,
            "lifecycle": {"status": "NOT_CHECKED"},
            "deprecation": known_deprecation(component),
            "vulnerability_status": "SKIPPED" if not online else "VERSION_NOT_RESOLVED",
            "vulnerabilities": [],
        }
        if online and version and group and artifact:
            if component.get("latest_lookup_supported") is False or group in {"unknown", "gradle.plugin"}:
                result["status"] = "NOT_SUPPORTED_BY_MAVEN_CENTRAL_LOOKUP"
            else:
                latest = latest_map.get((group, artifact), {"status": "UNAVAILABLE", "version": None})
                result["status"] = latest.get("status", "UNAVAILABLE")
                result["latest_version"] = latest.get("version")
                result["latest_source_url"] = latest.get("url")
                if latest.get("version"):
                    if not version_numbers(version) or not version_numbers(latest["version"]):
                        result["update_available"] = None
                        result["status"] = "COMPARISON_UNSUPPORTED"
                    else:
                        comparison = compare_versions(version, latest["version"])
                        if comparison < 0:
                            result["update_available"] = True
                            result["change_level"] = change_level(version, latest["version"])
                            recommendations.append({
                                "component": group + ":" + artifact,
                                "module": component.get("_module_path"),
                                "current_version": version,
                                "recommended_version": latest["version"],
                                "change_level": result["change_level"],
                                "reason": "A newer stable release was found in Maven Central metadata; compatibility was not assessed.",
                                "source_url": latest.get("url"),
                            })
                        elif comparison == 0:
                            result["update_available"] = False
                        else:
                            result["update_available"] = None
                            result["status"] = "CURRENT_VERSION_NEWER_THAN_REGISTRY"
                elif latest.get("message"):
                    result["lookup_message"] = latest["message"]

            slug = lifecycle_slug_for_component(component)
            if slug:
                result["lifecycle"] = lifecycle_status(lifecycle_product_map.get(slug, {"status": "UNAVAILABLE"}), slug, version)
                if result["lifecycle"].get("status") in {"EOL", "NOT_MAINTAINED", "OSS_SUPPORT_ENDED_EXTENDED_SUPPORT_AVAILABLE"}:
                    lifecycle_findings.append({
                        "component": group + ":" + artifact,
                        "module": component.get("_module_path"),
                        "version": version,
                        **result["lifecycle"],
                    })
            osv = osv_result.get("findings", {}).get(component_key)
            if osv is None:
                result["vulnerability_status"] = "NOT_CHECKED"
            else:
                result["vulnerability_status"] = osv.get("status", "UNAVAILABLE")
                result["vulnerabilities"] = osv.get("vulnerabilities", [])
                for vuln in result["vulnerabilities"]:
                    vulnerability_findings.append({
                        "component": group + ":" + artifact,
                        "module": component.get("_module_path"),
                        "version": version,
                        **vuln,
                    })
        if result["deprecation"].get("status") != "NOT_VERIFIED":
            deprecation_findings.append({
                "component": ((group or "unknown") + ":" + (artifact or "unknown")),
                "module": component.get("_module_path"),
                "version": version,
                **result["deprecation"],
            })
        component["version_intelligence"] = result

    for component in components:
        enrich_component(component)

    technology_updates = []
    for module in report["modules"]:
        updates = {}
        for key, label in (("java", "Java"), ("spring_boot", "Spring Boot")):
            detected = module[key].get("version")
            technology = {
                "status": "NOT_DETECTED" if not detected else ("SKIPPED" if not online else "CHECKED"),
                "current_version": detected,
                "recommended_version": None,
                "lifecycle": {"status": "NOT_CHECKED"},
                "source_note": None,
            }
            if online and detected:
                if key == "spring_boot":
                    latest = latest_map.get(("org.springframework.boot", "spring-boot-starter-parent"))
                    if latest and latest.get("version"):
                        technology["recommended_version"] = latest["version"]
                        technology["source_url"] = latest.get("url")
                        if compare_versions(detected, latest["version"]) < 0:
                            update = {
                                "component": "Spring Boot",
                                "module": module.get("path"),
                                "current_version": detected,
                                "recommended_version": latest["version"],
                                "change_level": change_level(detected, latest["version"]),
                                "reason": "Newer stable Spring Boot parent version found; verify Java compatibility and release notes before upgrading.",
                                "source_url": latest.get("url"),
                            }
                            recommendations.append(update)
                else:
                    technology["recommended_version"] = java_lts.get("recommended_version")
                    technology["source_url"] = java_lts.get("source_url")
                    technology["source_note"] = java_lts.get("basis")
                    current_major = java_major(detected)
                    recommended = java_lts.get("recommended_version")
                    if current_major and recommended and current_major.isdigit() and recommended.isdigit() and int(current_major) < int(recommended):
                        recommendations.append({
                            "component": "Java",
                            "module": module.get("path"),
                            "current_version": current_major,
                            "recommended_version": recommended,
                            "change_level": "JDK_MAJOR_VERSION_CHANGE_REVIEW_REQUIRED",
                            "reason": java_lts.get("basis"),
                            "source_url": java_lts.get("source_url"),
                        })
                slug = "spring-boot" if key == "spring_boot" else "java"
                product_data = lifecycle_product_map.get(slug, {"status": "UNAVAILABLE"})
                technology["lifecycle"] = lifecycle_status(product_data, slug, detected)
                if technology["lifecycle"].get("status") in {"EOL", "NOT_MAINTAINED", "OSS_SUPPORT_ENDED_EXTENDED_SUPPORT_AVAILABLE"}:
                    lifecycle_findings.append({
                        "component": label,
                        "module": module.get("path"),
                        "version": detected,
                        **technology["lifecycle"],
                    })
            updates[key] = technology
            technology_updates.append({"module": module.get("path"), "technology": label, **technology})
        module["technology_updates"] = updates

    # Remove private working fields from public inventory.
    for component in components:
        component.pop("_module_path", None)

    report["dependency_count"] = sum(1 for item in components if item.get("component_type") == "dependency")
    report["build_plugin_count"] = sum(1 for item in components if item.get("component_type") == "build_plugin")
    report["component_count"] = len(components)
    report["version_recommendations"] = recommendations
    report["lifecycle_findings"] = lifecycle_findings
    report["deprecation_findings"] = deprecation_findings
    report["vulnerability_findings"] = vulnerability_findings
    report["summary"] = {
        "outdated_component_count": len({(x.get("component"), x.get("current_version")) for x in recommendations}),
        "vulnerability_finding_count": len(vulnerability_findings),
        "affected_component_count": len({(x.get("component"), x.get("version")) for x in vulnerability_findings}),
        "lifecycle_warning_count": len({(x.get("component"), x.get("version"), x.get("status")) for x in lifecycle_findings}),
        "maintenance_advisory_count": len(deprecation_findings),
        "dependency_resolution_requested": report.get("dependency_resolution_requested", False),
    }
    report["version_intelligence"] = {
        "status": "COMPLETE" if online and osv_result.get("status") in {"COMPLETE", "NO_VERSIONED_COMPONENTS"} else ("PARTIAL" if online else "SKIPPED"),
        "checked_at": now_utc() if online else None,
        "latest_version_source": "Maven Central metadata",
        "vulnerability_source": "OSV.dev",
        "lifecycle_source": "endoflife.date where the product is catalogued",
        "java_lts_source": "Eclipse Adoptium available releases API",
        "vulnerability_scan": {key: value for key, value in osv_result.items() if key != "findings"},
        "notes": [
            "Latest available does not mean safest or compatible with this application.",
            "Known-vulnerability results only cover the package coordinates and versions that were resolved and queried.",
            "No advisory returned by OSV does not prove a component is vulnerability-free.",
            "EOL/lifecycle checks are available only for products catalogued by endoflife.date; Java support also depends on the JDK vendor.",
            "Dependency deprecation is reported only for explicit, source-backed advisory rules; otherwise it is marked NOT_VERIFIED.",
        ],
    }



def compact_report(report):
    """Return only components that require action or review; summarize clean components."""
    scan_info = report.get("version_intelligence", {})
    online = scan_info.get("status") != "SKIPPED"
    review_by_key = {}
    dependency_occurrences = 0
    components_ok = 0
    version_check_values = []
    vulnerability_check_values = []
    lifecycle_check_values = []

    def add_issue(item, issue):
        issue = {key: value for key, value in issue.items() if value is not None and value != "" and value != []}
        if issue not in item["issues"]:
            item["issues"].append(issue)

    def new_item(module, component, category, current_version=None, source=None):
        key = (module, component, category)
        if key not in review_by_key:
            review_by_key[key] = {
                "module": module,
                "component": component,
                "category": category,
                "current_version": current_version,
                "source_file": source,
                "issues": [],
            }
        elif not review_by_key[key].get("current_version") and current_version:
            review_by_key[key]["current_version"] = current_version
        if source and not review_by_key[key].get("source_file"):
            review_by_key[key]["source_file"] = source
        return review_by_key[key]

    def add_version_review(item, issue_type, message, latest=None, source_url=None):
        add_issue(item, {
            "type": issue_type,
            "severity": "REVIEW",
            "message": message,
            "latest_version": latest,
            "source_url": source_url,
        })

    modules = report.get("modules", [])
    module_count = report.get("module_count", len(modules))
    component_count_by_module = {}
    clean_component_keys = set()
    technology_count = 0
    technology_conflicts = {
        (item.get("technology"), tuple(item.get("versions", [])))
        for item in report.get("version_inconsistencies", [])
    }
    conflicted_technology_names = {item.get("technology") for item in report.get("version_inconsistencies", [])}
    conflicted_components = {item.get("component") for item in report.get("dependency_version_conflicts", [])}

    # Technology checks: only expose Java/Spring Boot when it needs attention.
    for module in modules:
        module_path = module.get("path", ".")
        module_component_count = len(module.get("dependencies", []))
        component_count_by_module[module_path] = module_component_count
        for field, label in (("java", "Java"), ("spring_boot", "Spring Boot")):
            found = module.get(field, {})
            intelligence = module.get("technology_updates", {}).get(field, {})
            current = found.get("version")
            recommended = intelligence.get("recommended_version")
            lifecycle_data = intelligence.get("lifecycle", {})
            lifecycle = lifecycle_data.get("status", "NOT_CHECKED")
            status = found.get("status", "NOT_DETECTED")
            if current:
                technology_count += 1

            item = None
            if status == "CONFLICT":
                item = new_item(module_path, label, "technology", current, None)
                add_issue(item, {
                    "type": "VERSION_CONFLICT",
                    "severity": "REVIEW",
                    "message": "Multiple versions of this technology were detected in this module.",
                    "versions": found.get("candidates", []),
                })
            elif current and recommended and version_numbers(current) and version_numbers(recommended):
                if compare_versions(current, recommended) < 0:
                    item = new_item(module_path, label, "technology", current, None)
                    add_issue(item, {
                        "type": "UPDATE_AVAILABLE",
                        "severity": "REVIEW",
                        "message": "A newer stable/LTS reference was found; confirm compatibility before updating.",
                        "recommended_version": recommended,
                        "source_url": intelligence.get("source_url"),
                    })
            elif current and recommended and (not version_numbers(current) or not version_numbers(recommended)):
                item = new_item(module_path, label, "technology", current, None)
                add_version_review(item, "VERSION_COMPARISON_UNCERTAIN", "The detected version cannot be compared reliably with the recommended version.", recommended, intelligence.get("source_url"))
            elif not current and label == "Java" and module_component_count:
                item = new_item(module_path, label, "technology", None, None)
                add_version_review(item, "VERSION_NOT_DETECTED", "No Java version was detected in this module's build configuration.")

            if current and lifecycle in {"EOL", "NOT_MAINTAINED", "OSS_SUPPORT_ENDED_EXTENDED_SUPPORT_AVAILABLE"}:
                item = item or new_item(module_path, label, "technology", current, None)
                add_issue(item, {
                    "type": "LIFECYCLE_WARNING",
                    "severity": "HIGH" if lifecycle == "EOL" else "WARNING",
                    "message": "Lifecycle status: " + lifecycle,
                    "support_end_date": lifecycle_data.get("eol_date"),
                    "source_url": lifecycle_data.get("source_url") or intelligence.get("source_url"),
                })

            if current:
                if online:
                    version_check_values.append("CHECKED" if recommended else "INCOMPLETE")
                    if lifecycle in {"MAINTAINED", "EOL", "NOT_MAINTAINED", "OSS_SUPPORT_ENDED_EXTENDED_SUPPORT_AVAILABLE"}:
                        lifecycle_check_values.append("CHECKED")
                    elif lifecycle in {"UNAVAILABLE", "NOT_CATALOGUED", "CYCLE_NOT_FOUND", "UNKNOWN"}:
                        lifecycle_check_values.append("INCOMPLETE")
                else:
                    version_check_values.append("SKIPPED")
                    lifecycle_check_values.append("SKIPPED")

    dependency_total = 0
    dependency_ok = 0
    plugin_total = 0
    conflict_items = []

    # Conflicts are review items; do not repeat the full dependency inventory.
    for conflict in report.get("dependency_version_conflicts", []):
        coordinate = conflict.get("component", "unknown")
        versions = conflict.get("versions", [])
        # The conflict can cross modules, so represent it as one project-level finding.
        item = new_item("*", coordinate, "dependency")
        add_issue(item, {
            "type": "VERSION_CONFLICT",
            "severity": "REVIEW",
            "message": "Different versions of the same component were found across modules.",
            "versions": versions,
        })
        conflict_items.append({"component": coordinate, "versions": versions, "status": "VERSION_CONFLICT"})

    for inconsistency in report.get("version_inconsistencies", []):
        coordinate = inconsistency.get("technology", "technology")
        versions = inconsistency.get("versions", [])
        item = new_item("*", coordinate, "technology")
        add_issue(item, {
            "type": "VERSION_CONFLICT",
            "severity": "REVIEW",
            "message": "Different versions were detected across modules.",
            "versions": versions,
        })
        conflict_items.append({"component": coordinate, "versions": versions, "status": "VERSION_VARIATION_ACROSS_MODULES"})

    # Analyze each component, but only retain actionable or uncertain findings.
    for module in modules:
        module_path = module.get("path", ".")
        for component in module.get("dependencies", []):
            dependency_total += 1
            if component.get("component_type") == "build_plugin":
                plugin_total += 1

            intelligence = component.get("version_intelligence", {})
            current = component.get("version")
            latest = intelligence.get("latest_version")
            update_available = intelligence.get("update_available")
            lookup_status = intelligence.get("status", "NOT_CHECKED")
            vuln_status = intelligence.get("vulnerability_status", "NOT_CHECKED")
            vulnerabilities = intelligence.get("vulnerabilities", [])
            lifecycle_data = intelligence.get("lifecycle", {})
            lifecycle = lifecycle_data.get("status", "NOT_CHECKED")
            deprecation = intelligence.get("deprecation", {})
            coordinate = (component.get("group_id") or "unknown") + ":" + (component.get("artifact_id") or "unknown")
            item = None
            component_has_problem = coordinate in conflicted_components

            if update_available is True:
                item = new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_issue(item, {
                    "type": "UPDATE_AVAILABLE",
                    "severity": "REVIEW",
                    "message": "A newer stable release is available; compatibility has not been assessed.",
                    "recommended_version": latest,
                    "source_url": intelligence.get("latest_source_url"),
                })
                component_has_problem = True
            elif not current:
                item = new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_version_review(item, "VERSION_UNKNOWN", "The dependency version could not be resolved from the project configuration.")
                component_has_problem = True
            elif online and lookup_status in {"UNAVAILABLE", "NOT_FOUND", "NO_STABLE_RELEASE_FOUND"}:
                item = new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_version_review(item, "VERSION_CHECK_INCOMPLETE", "The latest release could not be verified from the configured version source.", source_url=intelligence.get("latest_source_url"))
                component_has_problem = True
            elif online and lookup_status == "NOT_SUPPORTED_BY_MAVEN_CENTRAL_LOOKUP":
                item = new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_version_review(item, "VERSION_CHECK_NOT_SUPPORTED", "This component is not supported by the Maven Central version lookup; verify its publisher or repository.")
                component_has_problem = True
            elif online and lookup_status in {"COMPARISON_UNSUPPORTED", "CURRENT_VERSION_NEWER_THAN_REGISTRY"}:
                item = new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_version_review(item, "VERSION_COMPARISON_UNCERTAIN", "The current and latest versions could not be compared confidently.", latest, intelligence.get("latest_source_url"))
                component_has_problem = True
            elif online and lookup_status not in {"FOUND"} and update_available is not False:
                item = new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_version_review(item, "VERSION_NOT_VERIFIED", "The analyzer could not confirm whether this version is current.", latest, intelligence.get("latest_source_url"))
                component_has_problem = True

            if vulnerabilities:
                item = item or new_item(module_path, coordinate, "dependency", current, component.get("source"))
                for vulnerability in vulnerabilities:
                    add_issue(item, {
                        "type": "KNOWN_VULNERABILITY",
                        "severity": vulnerability.get("severity") or "UNKNOWN",
                        "advisory": vulnerability.get("id"),
                        "message": (vulnerability.get("summary") or "A known vulnerability advisory matches this version.")[:160],
                        "fixed_in": vulnerability.get("fixed_versions") or None,
                        "source_url": vulnerability.get("source_url"),
                    })
                component_has_problem = True
            elif online and current and vuln_status in {"UNAVAILABLE", "CHECK_UNAVAILABLE", "DETAILS_UNAVAILABLE"}:
                item = item or new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_version_review(item, "SECURITY_CHECK_INCOMPLETE", "The vulnerability check did not complete; security status is unknown.")
                component_has_problem = True
            elif online and current and vuln_status in {"NOT_CHECKED", "VERSION_NOT_RESOLVED", "SKIPPED"}:
                item = item or new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_version_review(item, "SECURITY_CHECK_INCOMPLETE", "This component was not covered by the vulnerability check.")
                component_has_problem = True

            if lifecycle in {"EOL", "NOT_MAINTAINED", "OSS_SUPPORT_ENDED_EXTENDED_SUPPORT_AVAILABLE"}:
                item = item or new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_issue(item, {
                    "type": "LIFECYCLE_WARNING",
                    "severity": "HIGH" if lifecycle == "EOL" else "WARNING",
                    "message": "Lifecycle status: " + lifecycle,
                    "support_end_date": lifecycle_data.get("eol_date"),
                    "source_url": lifecycle_data.get("source_url"),
                })
                component_has_problem = True

            if deprecation.get("status") not in {None, "NOT_VERIFIED"}:
                item = item or new_item(module_path, coordinate, "dependency", current, component.get("source"))
                add_issue(item, {
                    "type": deprecation.get("status"),
                    "severity": "WARNING",
                    "message": deprecation.get("reason") or deprecation.get("name") or "A maintenance advisory applies.",
                    "recommended_alternative": deprecation.get("recommended_alternative"),
                    "source_url": deprecation.get("source_url"),
                })
                component_has_problem = True

            if online:
                if current and lookup_status in {"FOUND"}:
                    version_check_values.append("CHECKED")
                elif not current or lookup_status not in {"FOUND"}:
                    version_check_values.append("INCOMPLETE")

                if current and vuln_status == "NO_MATCHING_ADVISORIES":
                    vulnerability_check_values.append("CHECKED")
                elif vuln_status in {"VULNERABLE"} and vulnerabilities:
                    vulnerability_check_values.append("CHECKED")
                else:
                    vulnerability_check_values.append("INCOMPLETE")

                if lifecycle not in {"NOT_CHECKED", "UNAVAILABLE", "NOT_CATALOGUED", "CYCLE_NOT_FOUND", "UNKNOWN"}:
                    lifecycle_check_values.append("CHECKED")
                elif lifecycle in {"UNAVAILABLE", "NOT_CATALOGUED", "CYCLE_NOT_FOUND"}:
                    lifecycle_check_values.append("INCOMPLETE")

            # A component is OK only when its version and security checks ran,
            # no update is pending, and no lifecycle/maintenance issue was found.
            if (
                online
                and not component_has_problem
                and current
                and update_available is False
                and vuln_status in {"NO_MATCHING_ADVISORIES", "NO_ADVISORY_REPORTED"}
            ):
                dependency_ok += 1

    # Normalize technology and component issues, and group repeated issues by component.
    review_items = []
    for item in review_by_key.values():
        if not item["issues"]:
            continue
        item["issues"] = sorted(
            item["issues"],
            key=lambda issue: (
                {"CRITICAL": 0, "HIGH": 1, "WARNING": 2, "REVIEW": 3, "UNKNOWN": 4, "INFO": 5}.get(issue.get("severity"), 6),
                issue.get("type", ""),
            ),
        )
        item["recommended_action"] = "; ".join(dict.fromkeys(
            action for action in (
                "Update and verify compatibility" if any(issue.get("type") == "UPDATE_AVAILABLE" for issue in item["issues"]) else None,
                "Review the security advisory and upgrade to a fixed version" if any(issue.get("type") == "KNOWN_VULNERABILITY" for issue in item["issues"]) else None,
                "Review support status and plan an upgrade" if any(issue.get("type") == "LIFECYCLE_WARNING" for issue in item["issues"]) else None,
                "Manually verify the version or check coverage" if any(issue.get("type") in {"VERSION_UNKNOWN", "VERSION_NOT_VERIFIED", "VERSION_CHECK_INCOMPLETE", "VERSION_CHECK_NOT_SUPPORTED", "VERSION_COMPARISON_UNCERTAIN", "VERSION_NOT_DETECTED", "SECURITY_CHECK_INCOMPLETE"} for issue in item["issues"]) else None,
                "Resolve the version conflict" if any(issue.get("type") == "VERSION_CONFLICT" for issue in item["issues"]) else None,
                "Review the recommended alternative" if any(issue.get("recommended_alternative") for issue in item["issues"]) else None,
            ) if action
        ))
        review_items.append(item)

    # Sort higher-risk findings first, then keep output deterministic.
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "WARNING": 2, "REVIEW": 3, "UNKNOWN": 4, "INFO": 5}
    review_items.sort(key=lambda item: (
        min((severity_rank.get(issue.get("severity"), 6) for issue in item["issues"]), default=6),
        item.get("module", ""),
        item.get("component", ""),
    ))

    # Online checking is not the same as having complete coverage.
    def check_state(values, online_enabled):
        if not online_enabled:
            return "SKIPPED"
        if not values:
            return "NOT_APPLICABLE"
        if any(value == "INCOMPLETE" for value in values):
            return "PARTIAL"
        return "CHECKED"

    version_check = check_state(version_check_values, online)
    vulnerability_check = check_state(vulnerability_check_values, online)
    lifecycle_check = check_state(lifecycle_check_values, online)

    if report.get("module_count", 0) == 0:
        overall_status = "NO_BUILD_FILES"
        status_message = "No supported build files were found."
    elif not online:
        overall_status = "CHECK_INCOMPLETE"
        status_message = "Online checks were skipped; no components are being marked OK."
    elif review_items:
        overall_status = "REVIEW_REQUIRED"
        if any(value in {"PARTIAL", "SKIPPED"} for value in (version_check, vulnerability_check, lifecycle_check)):
            status_message = str(len(review_items)) + " component(s) need review; some checks are incomplete. Components without detected issues are summarized as OK."
        else:
            status_message = str(len(review_items)) + " component(s) need review; components with no detected issues are summarized as OK."
    elif any(value in {"PARTIAL", "SKIPPED"} for value in (version_check, vulnerability_check, lifecycle_check)):
        overall_status = "CHECK_INCOMPLETE"
        status_message = "No issues were flagged in completed checks, but some checks were incomplete."
    else:
        overall_status = "OK"
        status_message = "All checked components look OK."

    known_issues = sum(
        1 for item in review_items for issue in item["issues"]
        if issue.get("type") == "KNOWN_VULNERABILITY"
    )
    update_items = sum(
        1 for item in review_items for issue in item["issues"]
        if issue.get("type") == "UPDATE_AVAILABLE"
    )
    lifecycle_items = sum(
        1 for item in review_items for issue in item["issues"]
        if issue.get("type") == "LIFECYCLE_WARNING"
    )
    conflict_count = sum(
        1 for item in review_items for issue in item["issues"]
        if issue.get("type") == "VERSION_CONFLICT"
    )
    java_spring_found = sum(
        1 for module in modules for key in ("java", "spring_boot")
        if module.get(key, {}).get("version")
    )
    scanned_components = dependency_total + technology_count

    no_build_files = module_count == 0
    scan = {
        "mode": "ONLINE" if online else "OFFLINE",
        "build_system": report.get("build_system", "Unknown"),
        "dependency_resolution": (
            "STATIC_ONLY" if not report.get("dependency_resolution_requested")
            else "NOT_CONFIRMED" if not modules
            else "RESOLVED" if all(module.get("dependency_resolution", {}).get("status") in {"RESOLVED", "NOT_APPLICABLE"} for module in modules)
            else "REQUESTED_BUT_INCOMPLETE"
        ),
        "project_detected": not no_build_files,
    }
    if no_build_files:
        scan["warning"] = "No Maven, Gradle, or Ant build file was found."

    components_needing_review = len(review_items)
    # Do not count components as OK when online checks were skipped or incomplete.
    components_ok = dependency_ok if online else 0
    if online:
        components_ok += sum(
            1 for module in modules
            for key in ("java", "spring_boot")
            if module.get(key, {}).get("version")
            and not any(
                item.get("module") == module.get("path")
                and item.get("category") == "technology"
                and item.get("component") == ("Java" if key == "java" else "Spring Boot")
                for item in review_items
            )
            and module.get(key, {}).get("status") != "CONFLICT"
            and ("Java" if key == "java" else "Spring Boot") not in conflicted_technology_names
        )

    return {
        "schema_version": "1.1",
        "project": report.get("project"),
        "scan": scan,
        "summary": {
            "status": overall_status,
            "message": status_message,
            "modules": module_count,
            "components_scanned": scanned_components,
            "components_ok": components_ok,
            "components_needing_review": components_needing_review,
            "updates_available": update_items,
            "known_vulnerabilities": known_issues,
            "lifecycle_warnings": lifecycle_items,
            "version_conflicts": conflict_count,
        },
        "review_items": review_items,
        "checks": {
            "latest_versions": version_check,
            "vulnerabilities": vulnerability_check,
            "lifecycle": lifecycle_check,
        },
    }

def audit(project, online=True, resolve=False):
    root = Path(project).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("Project path must be an existing directory: " + str(root))

    groups = {}
    for path in root.rglob("*"):
        if path.is_file() and path.name in BUILD and not any(part in SKIP for part in path.parts):
            groups.setdefault(path.parent, []).append(path)

    modules = []
    for folder, build_files in sorted(groups.items(), key=lambda item: str(item[0])):
        java_items = []
        boot_items = []
        components = []
        for path in sorted(build_files):
            if path.name == "pom.xml":
                java, boot, found = maven(path, root)
            elif path.name.startswith(("build.gradle", "settings.gradle")):
                java, boot, found = gradle(path, root)
            else:
                java, boot, found = ant(path, root)
            java_items.extend(java)
            boot_items.extend(boot)
            components.extend(found)

        relative_folder = str(folder.relative_to(root)) if folder != root else "."
        module = {
            "name": folder.name if folder != root else root.name,
            "path": relative_folder,
            "build_systems": sorted({BUILD[path.name] for path in build_files}),
            "build_files": [str(path.resolve().relative_to(root)) for path in build_files],
            "java": finding(java_items),
            "spring_boot": finding(boot_items),
            "dependencies": components,
        }
        if resolve:
            if "Maven" in module["build_systems"]:
                resolution = resolve_maven_tree(folder)
            elif "Gradle" in module["build_systems"]:
                resolution = resolve_gradle_tree(folder)
            else:
                resolution = {"status": "NOT_APPLICABLE", "components": [], "message": "Ant resolution is not implemented."}
            apply_resolution(module, resolution)
        else:
            module["dependency_resolution"] = {
                "status": "NOT_REQUESTED",
                "message": "Use --resolve to invoke Maven/Gradle and inspect the resolved dependency graph.",
                "resolved_component_count": 0,
            }
        modules.append(module)

    systems = sorted({system for module in modules for system in module["build_systems"]})
    inconsistencies = []
    for key, label in (("java", "Java"), ("spring_boot", "Spring Boot")):
        versions = sorted({module[key]["version"] for module in modules if module[key]["status"] == "DETECTED"})
        if len(versions) > 1:
            inconsistencies.append({
                "technology": label,
                "type": "VERSION_VARIATION_ACROSS_MODULES",
                "versions": versions,
            })

    component_versions = {}
    for module in modules:
        for component in module["dependencies"]:
            key = component.get("group_id", "") + ":" + component.get("artifact_id", "")
            component_versions.setdefault(key, set())
            if component.get("version"):
                component_versions[key].add(component["version"])
    dependency_conflicts = [
        {"component": key, "versions": sorted(versions)}
        for key, versions in sorted(component_versions.items()) if len(versions) > 1
    ]

    report = {
        "project": root.name,
        "project_path": str(root),
        "build_system": "Unknown" if not systems else systems[0] if len(systems) == 1 else "Multiple",
        "detected_build_systems": systems,
        "module_count": len(modules),
        "modules": modules,
        "dependency_version_conflicts": dependency_conflicts,
        "version_inconsistencies": inconsistencies,
        "dependency_resolution_requested": bool(resolve),
    }
    enrich_report(report, online=online)
    return compact_report(report)


def cli():
    parser = argparse.ArgumentParser(
        description="Audit Java technology/dependency versions, available updates, lifecycle status, and known vulnerabilities."
    )
    parser.add_argument("project", help="Path to the Java project directory")
    parser.add_argument("-o", "--output", help="Optional JSON report path (prints JSON when omitted)")
    parser.add_argument(
        "--offline", action="store_true",
        help="Disable online release, lifecycle, and vulnerability lookups",
    )
    parser.add_argument(
        "--resolve", action="store_true",
        help="Run Maven/Gradle dependency resolution to include resolved/transitive dependencies. This executes the project's build tool and build configuration.",
    )
    args = parser.parse_args()
    project = Path(args.project).expanduser()
    if not project.exists():
        parser.error("Project path does not exist: " + str(project))
    if not project.is_dir():
        parser.error("Project path must be a directory: " + str(project))

    try:
        report = audit(project, online=not args.offline, resolve=args.resolve)
    except ValueError as error:
        parser.error(str(error))
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print("Audit report written to " + str(output))
    else:
        print(rendered)


if __name__ == "__main__":
    cli()
