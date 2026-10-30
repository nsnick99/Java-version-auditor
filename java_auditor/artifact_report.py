"""Report for a packaged application (.war/.jar/.ear/.zip)."""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from .artifact_build import collect_classes, detect_app_name, detect_app_version, detect_build, detect_java
from .artifact_components import classify_libraries, declared_vs_present, find_version_conflicts, inventory_libraries
from .archive_source import open_source
from .bytecode_scanner import find_unresolved_classes, scan_internal_apis, scan_jdk_apis, scan_missing_files, scan_namespaces
from .config import AUDITOR_VERSION, FRAMEWORK_RULES, MINIMUM_JAVA_VERSION
from .migration import build_migration
from .utils import progress
from .version_checker import fetch_metadata_for, version_advice

RULES_DIR = Path(__file__).parent / "rules"
_STATUS = {"OUTDATED": "NEEDS_UPDATE", "END_OF_LIFE": "NEEDS_CHANGES", "OK": "CURRENT"}
_UPDATE_STATES = {"NEEDS_UPDATE", "MAJOR_UPDATE_AVAILABLE", "NEEDS_CHANGES"}


def _load(name): return json.loads((RULES_DIR / name).read_text(encoding="utf-8"))


def _lib_class_sets(libs):
    sets = []
    for lib in libs:
        nested = lib.get("_nested")
        if nested: sets.append({n[:-6] for n in nested.names() if n.endswith(".class") and not n.startswith("META-INF/versions/")})
    return sets


def build_artifact_report(path, target_java):
    path = Path(path).expanduser(); progress(f"Opening {path} ..."); src = open_source(str(path))
    jdk_rules, ns_rules = _load("jdk_removed.json"), _load("namespaces.json"); lib_rules, hints = _load("libraries.json"), _load("classpath_hints.json")
    progress("Detecting build type and versions..."); build_info, pom, gradle, manifest = detect_build(src)
    class_pairs = collect_classes(src); java_info = detect_java(src, pom, gradle, [ci for _p, ci in class_pairs]); libs = inventory_libraries(src)
    declared = (pom or {}).get("dependencies") or (gradle or {}).get("dependencies") or []
    progress("Classifying libraries..."); components = classify_libraries(libs, lib_rules)
    direct = {(d["groupId"], d["artifactId"]) for d in declared}
    for c in components: c["direct"] = (c["groupId"], c["artifactId"]) in direct if declared else None
    progress("Checking latest versions on Maven Central (skipped silently if unreachable)...")
    coords = {(c["groupId"], c["artifactId"]) for c in components if c.get("groupId") and c.get("version")}; metadata = fetch_metadata_for(coords) if coords else {}
    for c in components:
        advice = version_advice(c.get("version"), metadata.get((c.get("groupId"), c["artifactId"]), {"versions": []})); rule_state = _STATUS.get(c["status"], "CURRENT"); central_state = advice["version_status"]
        c["version_status"] = rule_state if rule_state != "CURRENT" else (central_state if central_state in _UPDATE_STATES else "CURRENT")
        c["latest_version"] = advice["latest_version"]; c["recommended_version"] = advice["recommended_version"] if central_state in _UPDATE_STATES else c.get("recommendedVersion") or "Unavailable"
        if c.get("recommendedVersion"): c["migration_target"] = c["recommendedVersion"]
    progress("Scanning bytecode and configuration..."); skip = list(hints["jdkPrefixes"])
    if build_info.get("packaging") == "war" or src.kind == "WAR": skip += hints["containerProvidedPrefixes"]
    missing_deps = declared_vs_present(declared, libs) if libs else []; unresolved = find_unresolved_classes(class_pairs, _lib_class_sets(libs), hints["hints"], skip)
    missing_files = [{"path": m["path"], "referenced_by": m["referencedBy"], "reason": m["kind"] + " not found in the package", "severity": m["severity"]} for m in scan_missing_files(src)]
    conflicts = find_version_conflicts(libs)
    api = {"deprecatedApis": scan_jdk_apis(class_pairs, jdk_rules, target_java), "internalApiUsage": scan_internal_apis(class_pairs, jdk_rules), "namespaceMigration": scan_namespaces(class_pairs, ns_rules)}
    for group in api.values(): group.sort(key=lambda x: ({"BLOCKER": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}[x["severity"]], x["ruleId"]))
    current_java = java_info.get("compiledAs", "Not detected").replace("Java ", ""); java_gap = {"current": current_java, "target": str(target_java)} if current_java.isdigit() and int(current_java) < target_java else None
    outdated = [{"name": c["artifactId"], "current": c["version"], "recommended": c["recommended_version"], "kind": "END_OF_LIFE" if c["status"] == "END_OF_LIFE" else "OUTDATED", "requires_jakarta": bool(c.get("requiresJakarta"))} for c in components if c["version_status"] in _UPDATE_STATES]
    migration = build_migration(target_java, api, missing_files, missing_deps, unresolved, outdated, conflicts, java_gap)
    issues = []
    for item in missing_files: issues.append({"title": "Missing referenced file or project", "severity": item["severity"], "path": item["path"], "message": item["reason"]})
    for d in missing_deps: issues.append({"title": "Declared dependency missing from package", "severity": "HIGH", "path": d["artifact"], "message": d["reason"]})
    for u in unresolved: issues.append({"title": "Unresolved class reference", "severity": "HIGH", "path": u["class"], "message": "Referenced by " + ", ".join(u["referencedBy"]) + (f"; likely artifact {u['likelyArtifact']}" if u.get("likelyArtifact") else "")})
    for c in conflicts: issues.append({"title": "Library version conflict", "severity": "MEDIUM", "path": c["artifact"], "message": "Multiple versions packaged: " + ", ".join(c["versionsFound"])})
    issues.sort(key=lambda x: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(x["severity"], 3), x["title"]))
    fw_index = {(g, a): name for name, pairs in FRAMEWORK_RULES for g, a in pairs}
    frameworks = [{"name": fw_index[(c["groupId"], c["artifactId"])], "version": c["version"], "version_status": c["version_status"], "recommended_version": c["recommended_version"], "module": src.label, "change_reason": c.get("notes")} for c in components if (c.get("groupId"), c["artifactId"]) in fw_index and c["version_status"] in _UPDATE_STATES]
    dependencies = [{"name": f"{c['groupId']}:{c['artifactId']}" if c.get("groupId") else c["artifactId"], "version": c["version"], "type": "direct" if c.get("direct") else "packaged", "latest_version": c["latest_version"], "recommended_version": c["recommended_version"], "version_status": c["version_status"], "notes": c.get("notes"), "file": c.get("file")} for c in components if c["version_status"] in _UPDATE_STATES]
    recommendations = [{"component": d["name"], "current_version": d["version"], "recommended_version": d["recommended_version"], "reason": d.get("notes") or "A newer release is available; review release notes before upgrading."} for d in dependencies]
    if java_gap: recommendations.insert(0, {"component": "Java", "current_version": current_java, "recommended_version": str(target_java), "version_status": "NEEDS_CHANGES", "reason": f"Application is compiled for Java {current_java}; target is Java {target_java}."})
    return {"auditor_version": AUDITOR_VERSION, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "project_path": str(path.resolve()), "project_name": detect_app_name(manifest, pom, str(path)), "input_type": src.kind, "application_version": detect_app_version(manifest, pom, src), "build_system": build_info.get("tool") or "Unknown", "build": build_info, "current_java": current_java, "java": java_info, "minimum_java": MINIMUM_JAVA_VERSION, "scan_summary": {"class_files_scanned": len(class_pairs), "library_jars_found": len(libs), "dependencies_with_updates": len(dependencies), "missing_references_found": len(missing_files) + len(missing_deps) + len(unresolved)}, "frameworks": frameworks, "dependencies": dependencies, "missing_files": missing_files, "missing_dependencies": missing_deps, "unresolved_classes": unresolved, "api_findings": api, "dependency_conflicts": conflicts, "issues": issues, "recommendations": recommendations, "migration": migration, "analysis_notes": ["Packaged-application mode: findings come from bytecode, the library inventory and config files.", "Latest versions use Maven Central when reachable; otherwise built-in rules are used."]}
