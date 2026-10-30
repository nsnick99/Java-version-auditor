"""Migration summary and ordered plan, shared by source and artifact reports."""

from __future__ import annotations

SEVERITIES = ("BLOCKER", "HIGH", "MEDIUM", "LOW")
WEIGHT = {"BLOCKER": 15, "HIGH": 8, "MEDIUM": 3, "LOW": 1}


def _readiness(counts):
    score = 100 - sum(WEIGHT[s] * min(counts[s], 6) for s in SEVERITIES)
    score = max(0, min(100, score))
    level = ("READY" if score >= 85 else "MINOR_WORK" if score >= 65
             else "NEEDS_SIGNIFICANT_WORK" if score >= 40 else "MAJOR_REWORK")
    return score, level


def build_migration(target_java, api, missing_files, missing_dependencies, unresolved_classes,
                    outdated, conflicts, java_gap):
    counts = {s: 0 for s in SEVERITIES}
    for group in api.values():
        for item in group:
            counts[item["severity"]] += 1
    for item in missing_files:
        counts[item.get("severity", "HIGH")] += 1
    counts["HIGH"] += len(missing_dependencies) + (1 if unresolved_classes else 0)
    for item in outdated:
        counts["HIGH" if item["kind"] in {"END_OF_LIFE", "NEEDS_CHANGES"} else "MEDIUM"] += 1
    counts["MEDIUM"] += len(conflicts)
    if java_gap:
        counts["HIGH" if int(java_gap["current"]) < 8 else "MEDIUM"] += 1

    steps = []

    def add(action, refs):
        refs = list(dict.fromkeys(r for r in refs if r))
        if refs:
            steps.append({"step": len(steps) + 1, "action": action, "refs": refs})

    add("Fix missing dependencies, unresolved classes and missing files so the build is trustworthy",
        (["missing_files"] if missing_files else [])
        + (["missing_dependencies"] if missing_dependencies else [])
        + (["unresolved_classes"] if unresolved_classes else []))
    add("Remove internal JDK API usage (sun.*, Unsafe)", [x["ruleId"] for x in api["internalApiUsage"]])
    add(f"Raise the Java baseline to {target_java} and add replacement libraries for removed JDK modules",
        (["java_version"] if java_gap else [])
        + [x["ruleId"] for x in api["deprecatedApis"] if x["category"] == "JDK_REMOVED"])
    add("Replace APIs deprecated for removal",
        [x["ruleId"] for x in api["deprecatedApis"] if x["category"] != "JDK_REMOVED"])
    add("Run the javax.* to jakarta.* namespace migration (e.g. OpenRewrite) and upgrade the frameworks that require it",
        [x["ruleId"] for x in api["namespaceMigration"]]
        + ["component:" + o["name"] for o in outdated if o.get("requires_jakarta")])
    add("Upgrade other outdated libraries, replace end-of-life ones and resolve version conflicts",
        ["component:" + o["name"] for o in outdated if not o.get("requires_jakarta")]
        + ["conflict:" + c["artifact"] for c in conflicts])

    score, level = _readiness(counts)
    return {"target_java": str(target_java),
            "summary": {"readiness_score": score, "readiness_level": level, "counts": counts},
            "plan": steps}
