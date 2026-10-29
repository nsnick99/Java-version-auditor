"""Coordinates scanning and builds the final JSON report."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .build_files import read_ant, read_gradle, read_pom
from .config import AUDITOR_VERSION, BUILD_NAMES, MINIMUM_JAVA_VERSION, SETTINGS_NAMES
from .dependency_resolver import merge_dependencies, resolve_dependency_tree, resolve_maven_updates
from .project_scanner import detect_missing_gradle_modules, scan_deprecated_apis
from .utils import find_files, java_major, progress, rel, version_sort_key, version_tuple
from .version_checker import fetch_metadata_for, identify_frameworks, version_advice

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

    progress(f"Scanning project: {root}")
    progress("Discovering build files and modules...")
    build_files = find_files(root, BUILD_NAMES)
    settings_files = find_files(root, SETTINGS_NAMES)
    progress(f"Found {len(build_files)} build files and {len(settings_files)} Gradle settings files.")
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
    for module_index, info in enumerate(module_infos, 1):
        progress(f"[{module_index}/{len(module_infos)}] Resolving dependencies for {rel(info['path'], root)} ({info.get('build_system')})...")
        if info.get("is_aggregator") and not info["direct_dependencies"]:
            tree, resolution_error = [], None
        else:
            tree, resolution_error = resolve_dependency_tree(info, root)
        if info.get("build_system") == "Maven" and not (info.get("is_aggregator") and not info["direct_dependencies"]):
            progress(f"[{module_index}/{len(module_infos)}] Checking Maven dependency and parent updates...")
            update_data, update_error = resolve_maven_updates(info, root)
            if update_data:
                maven_update_records.append(update_data)
            elif update_error:
                resolution_notes.append({"module": rel(info["path"].parent, root), "message": update_error})
                issues.append({
                    "title": "Latest-version check incomplete",
                    "severity": "MEDIUM",
                    "path": rel(info["path"], root),
                    "message": update_error + ". Newer dependency or parent versions may be missing from this report.",
                })
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
    progress(f"Checking latest-version metadata for {len(metadata_coordinates)} dependency coordinates...")
    metadata_cache = fetch_metadata_for(metadata_coordinates)
    progress("Version metadata lookup finished.")

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

    dependency_status_counts = {}
    for dep in dependencies:
        status = dep.get("version_status") or "UNKNOWN"
        dependency_status_counts[status] = dependency_status_counts.get(status, 0) + 1

    frameworks = []
    for info in module_infos:
        module_key = rel(info["path"].parent, root)
        module_deps = [d for d in dependencies if d.get("module") == module_key]
        frameworks.extend(identify_frameworks(module_deps, info, metadata_cache, root))

    framework_unique = {}
    for item in frameworks:
        framework_unique[(item["name"], item.get("module"), item["version"])] = item
    frameworks = sorted(framework_unique.values(), key=lambda x: (x["name"], str(x.get("module")), str(x.get("version"))))
    frameworks_detected_count = len(frameworks)

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

    progress("Scanning Java source for selected deprecated API patterns...")
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

    for item in frameworks:
        if item.get("version_status") != "NEEDS_CHANGES" or item.get("name") != "Spring Boot":
            continue
        module = next((m for m in module_details if m.get("path") == item.get("module")), None)
        current_java_version = java_major(module.get("java_version")) if module else None
        if current_java_version and int(current_java_version) < 17:
            recommendations.append({
                "component": "Java runtime",
                "module": item.get("module"),
                "current_version": current_java_version,
                "recommended_version": "17",
                "version_status": "NEEDS_CHANGES",
                "reason": f"Spring Boot {item['version']} requires Java 17 or newer.",
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
        "auditor_version": AUDITOR_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_path": str(root),
        "project_name": root.name,
        "build_system": build_system,
        "current_java": current_java,
        "minimum_java": MINIMUM_JAVA_VERSION,
        "scan_summary": {
            "build_files_found": len(build_files),
            "modules_analyzed": len(module_details),
            "dependencies_detected": len(dependencies),
            "dependencies_with_updates": sum(
                1 for dep in dependencies
                if dep.get("version_status") in {"NEEDS_UPDATE", "MAJOR_UPDATE_AVAILABLE"}
            ),
            "dependency_status_counts": dict(sorted(dependency_status_counts.items())),
            "frameworks_detected_before_update_filter": frameworks_detected_count,
            "framework_updates_reported": len(frameworks),
            "missing_references_found": len(missing_files),
            "dependency_resolution_diagnostics": resolution_notes,
        },
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
        ] + ([f"Dependency/version checks were incomplete for {len(resolution_notes)} module(s); see Issues for the diagnostic."] if resolution_notes else []),
    }
