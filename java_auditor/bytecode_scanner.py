"""Rule-driven scanners over parsed class files and config files."""

import re
from collections import defaultdict

MAX_LOCATIONS = 5


def _dot(name):
    return name.replace("/", ".")


def _rule_inactive(rule, target_java):
    if target_java is None: return False
    if "removedIn" in rule and target_java < rule["removedIn"]: return True
    if "deprecatedIn" in rule and target_java < rule["deprecatedIn"]: return True
    return False


def scan_jdk_apis(class_infos, rules, target_java):
    hits = defaultdict(lambda: {"occurrences": 0, "classes": set(), "apis": set()})
    for _path, ci in class_infos:
        for rule in rules["rules"]:
            if _rule_inactive(rule, target_java): continue
            for prefix in rule.get("prefixes", []):
                for ref in ci.class_refs:
                    if ref.startswith(prefix):
                        h = hits[rule["id"]]; h["occurrences"] += 1; h["classes"].add(_dot(ci.name)); h["apis"].add(_dot(ref))
            for member in rule.get("members", []):
                owner, _, name = member.rpartition(".")
                if (owner, name) in ci.member_refs:
                    h = hits[rule["id"]]; h["occurrences"] += 1; h["classes"].add(_dot(ci.name)); h["apis"].add(f"{_dot(owner)}.{name}")
    by_id = {r["id"]: r for r in rules["rules"]}; findings = []
    for rid, h in hits.items():
        rule = by_id[rid]; f = {"ruleId": rid, "severity": rule["severity"], "category": rule["category"], "apis": sorted(h["apis"])[:MAX_LOCATIONS], "occurrences": h["occurrences"], "classesAffected": len(h["classes"]), "locations": [{"class": c} for c in sorted(h["classes"])[:MAX_LOCATIONS]], "replacement": rule["replacement"]}
        if "removedIn" in rule: f["removedIn"] = str(rule["removedIn"])
        if "deprecatedIn" in rule: f["deprecatedIn"] = str(rule["deprecatedIn"])
        findings.append(f)
    return findings


def scan_internal_apis(class_infos, rules):
    hits = defaultdict(lambda: {"occurrences": 0, "classes": set(), "apis": set()})
    for _path, ci in class_infos:
        matched = set()
        for rule in rules["internalPrefixes"]:
            for ref in ci.class_refs:
                if any(ref.startswith(p) for p in rule["prefixes"]):
                    key = "INT-UNSAFE" if ref.startswith("sun/misc/Unsafe") else rule["id"]
                    if rule["id"] != key or (key, ref) in matched: continue
                    matched.add((key, ref)); h = hits[key]; h["occurrences"] += 1; h["classes"].add(_dot(ci.name)); h["apis"].add(_dot(ref))
    by_id = {r["id"]: r for r in rules["internalPrefixes"]}; findings = []
    for rid, h in hits.items():
        rule = by_id[rid]
        findings.append({"ruleId": rid, "severity": rule["severity"], "apis": sorted(h["apis"])[:MAX_LOCATIONS], "occurrences": h["occurrences"], "classesAffected": len(h["classes"]), "locations": [{"class": c} for c in sorted(h["classes"])[:MAX_LOCATIONS]], "suggestedAlternative": rule["suggestedAlternative"]})
    return findings


def scan_namespaces(class_infos, rules):
    hits = defaultdict(lambda: {"classes": set(), "occurrences": 0})
    for _path, ci in class_infos:
        for m in rules["mappings"]:
            for ref in ci.class_refs:
                if ref.startswith(m["from"]):
                    h = hits[(m["from"], m["to"])]; h["classes"].add(_dot(ci.name)); h["occurrences"] += 1
    return [{"ruleId": "NS-" + src.strip("/").split("/")[-1].upper(), "severity": rules["severity"], "from": _dot(src) + "*", "to": _dot(dst) + "*", "filesAffected": len(h["classes"]), "occurrences": h["occurrences"], "reason": f"Required by {rules['requiredBy']}", "locations": [{"class": c} for c in sorted(h["classes"])[:MAX_LOCATIONS]]} for (src, dst), h in sorted(hits.items())]


def find_unresolved_classes(class_infos, lib_class_sets, hints, jdk_prefixes):
    app_classes = {ci.name for _p, ci in class_infos}; lib_classes = set().union(*lib_class_sets) if lib_class_sets else set(); jdk = tuple(jdk_prefixes); unresolved = defaultdict(set)
    for _p, ci in class_infos:
        for ref in ci.class_refs:
            if ref in app_classes or ref in lib_classes or ref.startswith(jdk): continue
            unresolved[ref].add(_dot(ci.name))
    out = []
    for ref, referrers in sorted(unresolved.items()):
        likely = next((h["artifact"] for h in hints if ref.startswith(h["prefix"])), None); entry = {"class": _dot(ref), "referencedBy": sorted(referrers)[:MAX_LOCATIONS]}
        if likely: entry["likelyArtifact"] = likely
        out.append(entry)
    return out


CONFIG_REFS = [(re.compile(r'classpath\*?:([\w./\-*${}]+\.\w+)'), "Spring classpath reference", 1), (re.compile(r'<(?:jar-file|mapping-file)>\s*([^<\s]+)\s*<'), "persistence.xml reference", 1), (re.compile(r'<import\s+resource="([^"]+)"'), "Spring XML import", 1)]


def scan_missing_files(src):
    names = src.names(); name_set = set(names); suffix_index = {n.rsplit("/", 1)[-1] for n in names}; issues = []
    config_files = [n for n in names if n.lower().endswith((".xml", ".properties", ".yml", ".yaml")) and ("WEB-INF/" in n or "META-INF/" in n or "classes/" in n or n.count("/") <= 2) and "META-INF/maven/" not in n]
    for cf in config_files[:200]:
        text = src.read_text(cf); base_dirs = ["", "WEB-INF/classes/", "BOOT-INF/classes/"]
        for pat, desc, grp in CONFIG_REFS:
            for m in pat.finditer(text):
                ref = m.group(grp)
                if "${" in ref or "*" in ref or ref.startswith("http"): continue
                ref = re.sub(r"^classpath\*?:", "", ref).lstrip("/")
                candidates = [ref] + [b + ref for b in base_dirs]
                if desc == "Spring XML import" and "/" in cf: candidates.insert(0, cf.rsplit("/", 1)[0] + "/" + ref)
                if not any(c in name_set for c in candidates) and ref.rsplit("/", 1)[-1] not in suffix_index:
                    issues.append({"path": ref, "referencedBy": cf, "kind": desc, "severity": "HIGH"})
    if any(n.startswith("WEB-INF/") for n in names) and "WEB-INF/web.xml" not in name_set and not any(n.startswith("BOOT-INF/") for n in names):
        issues.append({"path": "WEB-INF/web.xml", "referencedBy": "WAR layout", "kind": "Servlet deployment descriptor (optional with Servlet 3+ annotations)", "severity": "LOW"})
    seen, deduped = set(), []
    for i in issues:
        key = (i["path"], i["referencedBy"])
        if key not in seen: seen.add(key); deduped.append(i)
    return deduped
