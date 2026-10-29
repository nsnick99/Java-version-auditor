"""Dependency-tree and Maven version-plugin resolution."""

from __future__ import annotations

import re

from .config import PLACEHOLDER_START, PRE_RELEASE, TOOL_TIMEOUT
from .utils import (
    clean, executable_or_wrapper, progress, rel, run_command,
    version_sort_key, version_tuple,
)

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
    progress(f"Running Maven Versions Plugin for {rel(info['path'], root)} (timeout: {TOOL_TIMEOUT}s)...")
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
        progress(f"Running Maven dependency tree for {rel(info['path'], root)} (timeout: {TOOL_TIMEOUT}s)...")
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
        progress(f"Running Gradle dependency report for {rel(info['path'], root)} (timeout: {TOOL_TIMEOUT}s)...")
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
