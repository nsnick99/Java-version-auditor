"""Maven, Gradle, and Ant build-file parsing."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .utils import (
    child, clean, interpolate, java_major, local_name, node_text, rel,
    resolve_path_expression,
)

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
