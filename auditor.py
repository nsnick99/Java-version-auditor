from __future__ import annotations

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SKIP_DIRS = {".git", "target", "build", "out", "bin", "node_modules", ".gradle", "__pycache__", ".venv", "venv"}
BUILD_FILES = {"pom.xml": "Maven", "build.gradle": "Gradle", "build.gradle.kts": "Gradle", "settings.gradle": "Gradle", "settings.gradle.kts": "Gradle"}

FRAMEWORKS = {
    "Spring Boot": {"org.springframework.boot": {"spring-boot-starter-web", "spring-boot-starter-data-jpa", "spring-boot-starter-data-mongodb", "spring-boot-starter-security", "spring-boot-starter-test", "spring-boot-devtools"}},
    "Spring MVC": {"org.springframework": {"spring-webmvc", "spring-web"}},
    "Spring Security": {"org.springframework.security": set()},
    "Hibernate": {"org.hibernate": {"hibernate-core"}, "org.hibernate.orm": {"hibernate-core"}},
    "Struts": {"org.apache.struts": set()},
    "JSF": {"javax.faces": set(), "jakarta.faces": set()},
    "Apache Camel": {"org.apache.camel": set()},
    "JPA": {"javax.persistence": {"javax.persistence-api"}, "jakarta.persistence": {"jakarta.persistence-api"}},
}


def local_name(tag):
    return tag.split("}")[-1]


def child(node, name):
    if node is None:
        return None
    return next((x for x in node if local_name(x.tag) == name), None)


def value(node, name):
    x = child(node, name)
    return (x.text or "").strip() if x is not None and x.text else None


def clean(v):
    if v is None:
        return None
    v = str(v).strip().strip("'\"")
    return None if not v or "${" in v else v


def java_major(v):
    v = clean(v)
    if not v:
        return None
    if v.startswith("1."):
        return v.split(".")[1]
    m = re.match(r"\d+", v)
    return m.group(0) if m else v


def interpolate(v, props):
    if not v:
        return None
    for _ in range(5):
        old = v
        v = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), v)
        if v == old:
            break
    return clean(v)


def version_key(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums)


def parse_xml(path):
    return ET.parse(path).getroot()


def parse_pom(path):
    root = parse_xml(path)
    props = {}
    prop_node = child(root, "properties")
    if prop_node is not None:
        for x in prop_node:
            if x.text:
                props[local_name(x.tag)] = x.text.strip()

    java_versions = []
    for key in ("java.version", "maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "jdk.version"):
        if key in props:
            v = java_major(interpolate(props[key], props))
            if v:
                java_versions.append(v)

    parent = child(root, "parent")
    boot_version = None
    if parent is not None and value(parent, "groupId") == "org.springframework.boot" and value(parent, "artifactId") == "spring-boot-starter-parent":
        boot_version = interpolate(value(parent, "version"), props)

    managed = {}
    dm = child(root, "dependencyManagement")
    dm_deps = child(dm, "dependencies")
    if dm_deps is not None:
        for d in dm_deps:
            if local_name(d.tag) != "dependency":
                continue
            g, a, v = value(d, "groupId"), value(d, "artifactId"), value(d, "version")
            if g and a and v:
                managed[(g, a)] = interpolate(v, props)

    deps = []
    dep_node = child(root, "dependencies")
    if dep_node is not None:
        for d in dep_node:
            if local_name(d.tag) != "dependency":
                continue
            g, a = value(d, "groupId"), value(d, "artifactId")
            if not g or not a:
                continue
            raw = value(d, "version")
            v = interpolate(raw, props) if raw else managed.get((g, a))
            deps.append({"group": g, "artifact": a, "version": v, "direct": bool(raw), "scope": value(d, "scope") or "compile"})

    return java_versions, boot_version, deps


def parse_gradle(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    java = []
    for m in re.finditer(r"(?:sourceCompatibility|targetCompatibility)\s*[=:]\s*['\"]?(?:JavaVersion\.)?VERSION_(?:1_)?(\d+)|JavaLanguageVersion\.of\(\s*(\d+)\s*\)", text, re.I):
        java.append(m.group(1) or m.group(2))

    boot = None
    m = re.search(r"id\s*['\"]org\.springframework\.boot['\"]\s*version\s*['\"]([^'\"]+)", text)
    if not m:
        m = re.search(r"org\.springframework\.boot:spring-boot-gradle-plugin:([^'\"]+)", text)
    if m:
        boot = clean(m.group(1))

    deps = []
    pattern = re.compile(r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|annotationProcessor)\s*\(?\s*['\"]([^:'\"]+):([^:'\"]+):([^'\"]+)['\"]")
    for m in pattern.finditer(text):
        deps.append({"group": m.group(1), "artifact": m.group(2), "version": clean(m.group(3)), "direct": True, "scope": "gradle"})
    return java, boot, deps


def fetch_boot_management(boot_version):
    if not boot_version:
        return {}
    url = f"https://repo.maven.apache.org/maven2/org/springframework/boot/spring-boot-dependencies/{boot_version}/spring-boot-dependencies-{boot_version}.pom"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Java-Version-Auditor/3.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            root = ET.fromstring(r.read())
        props = {}
        p = child(root, "properties")
        if p is not None:
            for x in p:
                if x.text:
                    props[local_name(x.tag)] = x.text.strip()
        managed = dict(props)
        dmn = child(child(root, "dependencyManagement"), "dependencies")
        if dmn is not None:
            for d in dmn:
                if local_name(d.tag) != "dependency":
                    continue
                g, a, v = value(d, "groupId"), value(d, "artifactId"), value(d, "version")
                if g and a and v:
                    managed[(g, a)] = interpolate(v, props)
        return managed
    except Exception:
        return {}


def resolve_dependency_versions(deps, managed):
    out = []
    for d in deps:
        item = dict(d)
        if not item.get("version"):
            item["version"] = managed.get((d["group"], d["artifact"]))
        out.append(item)
    return out


def framework_version(name, components, boot_version, boot_management):
    if name == "Spring Boot":
        return boot_version

    # Exact dependency version wins.
    versions = [c["version"] for c in components if c.get("version")]
    if versions:
        return sorted(set(versions), key=version_key)[-1]

    # For Spring Boot-managed frameworks, use the exact versions from the Boot BOM.
    candidates = {
        "Spring MVC": [("org.springframework", "spring-webmvc"), ("org.springframework", "spring-web")],
        "Spring Security": [("org.springframework.security", "spring-security-core"), ("org.springframework.security", "spring-security-web")],
        "Hibernate": [("org.hibernate", "hibernate-core"), ("org.hibernate.orm", "hibernate-core")],
        "JPA": [("javax.persistence", "javax.persistence-api"), ("jakarta.persistence", "jakarta.persistence-api")],
    }.get(name, [])
    for key in candidates:
        if key in boot_management and boot_management[key]:
            return boot_management[key]

    # Boot BOM properties are useful when the exact dependency isn't represented directly.
    prop_candidates = {
        "Spring MVC": ["spring-framework.version"],
        "Spring Security": ["spring-security.version"],
        "Hibernate": ["hibernate.version"],
        "JPA": ["javax-persistence.version", "jakarta-persistence.version"],
    }.get(name, [])
    for key in prop_candidates:
        if boot_management.get(key):
            return boot_management[key]
    return None


def identify_frameworks(deps, boot_version, boot_management):
    found = {}
    for d in deps:
        g, a = d["group"], d["artifact"]
        for name, rules in FRAMEWORKS.items():
            matched = False
            for rg, artifacts in rules.items():
                if g != rg:
                    continue
                matched = not artifacts or a in artifacts or any(a.startswith(x) for x in artifacts if x.endswith("-"))
                if matched:
                    break
            if matched:
                found.setdefault(name, []).append({"group": g, "artifact": a, "version": d.get("version")})

    # Spring Boot is identified from the parent/plugin even when no starter is present.
    if boot_version:
        found.setdefault("Spring Boot", [])

    result = []
    for name in sorted(found):
        result.append({"name": name, "version": framework_version(name, found[name], boot_version, boot_management)})
    return result


def analyze(project):
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Project directory not found: {root}")

    build_files = []
    for p in root.rglob("*"):
        if p.is_file() and p.name in BUILD_FILES and not any(part in SKIP_DIRS for part in p.parts):
            build_files.append(p)

    all_java, boot_versions, all_deps = [], [], []
    systems = set()
    for p in sorted(build_files):
        systems.add(BUILD_FILES[p.name])
        if p.name == "pom.xml":
            java, boot, deps = parse_pom(p)
            all_java.extend(java)
            if boot:
                boot_versions.append(boot)
            all_deps.extend(deps)
        elif p.name.startswith(("build.gradle", "settings.gradle")):
            java, boot, deps = parse_gradle(p)
            all_java.extend(java)
            if boot:
                boot_versions.append(boot)
            all_deps.extend(deps)

    java = sorted(set(all_java), key=version_key)
    boots = sorted(set(boot_versions), key=version_key)
    boot_version = boots[-1] if boots else None
    managed = fetch_boot_management(boot_version) if boot_version else {}
    deps = resolve_dependency_versions(all_deps, managed)
    frameworks = identify_frameworks(deps, boot_version, managed)

    # Keep the report deliberately small and human-readable.
    report = {
        "project_name": root.name,
        "build_system": next(iter(systems)) if len(systems) == 1 else ("Multiple" if systems else None),
        "current_java": java[-1] if java else None,
        "frameworks": frameworks,
        "dependencies": [
            {"name": f"{d['group']}:{d['artifact']}", "version": d.get("version")}
            for d in deps
        ],
    }
    return report


def main():
    if len(sys.argv) != 2:
        print("Usage: python auditor.py <java-project-path>")
        sys.exit(1)

    try:
        report = analyze(sys.argv[1])
        output = Path("output.json").resolve()
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Report created: {output}")
    except Exception as exc:
        print(f"Audit failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
