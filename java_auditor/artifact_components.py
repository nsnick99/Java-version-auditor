"""Library inventory: embedded jars, versions, conflicts, declared vs present."""

import re
from collections import defaultdict
from .archive_source import open_nested

LIB_DIRS = ("WEB-INF/lib/", "BOOT-INF/lib/", "lib/", "APP-INF/lib/")
JAR_NAME = re.compile(r"^(?P<artifact>.+?)-(?P<version>\d[\w.\-+]*)\.jar$")


def parse_jar_filename(filename):
    m = JAR_NAME.match(filename)
    return (m.group("artifact"), m.group("version")) if m else (filename[:-4] if filename.endswith(".jar") else filename, None)


def parse_pom_properties(text):
    props = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("="); props[k.strip()] = v.strip()
    return props


def version_tuple(v):
    if not v: return ()
    parts = []
    for tok in re.split(r"[.\-_+]", v):
        if tok.isdigit(): parts.append(int(tok))
        else:
            m = re.match(r"(\d+)", tok)
            if m: parts.append(int(m.group(1)))
            break
    return tuple(parts)


def inventory_libraries(src):
    libs = []
    for name in src.names():
        if not name.endswith(".jar") or not any(name.startswith(d) or ("/" + d) in name for d in LIB_DIRS): continue
        filename = name.rsplit("/", 1)[-1]; artifact, version = parse_jar_filename(filename)
        lib = {"file": name, "groupId": None, "artifactId": artifact, "version": version, "source": "filename"}
        nested = open_nested(src, name)
        if nested:
            pp = [n for n in nested.names() if n.startswith("META-INF/maven/") and n.endswith("/pom.properties")]
            if pp:
                props = parse_pom_properties(nested.read_text(pp[0])); lib["groupId"] = props.get("groupId"); lib["artifactId"] = props.get("artifactId", artifact); lib["version"] = props.get("version", version); lib["source"] = "pom.properties"
            else:
                mf = nested.read_text("META-INF/MANIFEST.MF") if "META-INF/MANIFEST.MF" in nested.names() else ""
                m = re.search(r"^Implementation-Version:\s*(\S+)", mf, re.M)
                if m and not lib["version"]: lib["version"] = m.group(1); lib["source"] = "manifest"
            lib["_nested"] = nested
        libs.append(lib)
    return libs


def find_version_conflicts(libs):
    by_artifact = defaultdict(list)
    for lib in libs: by_artifact[(lib["groupId"], lib["artifactId"])].append(lib)
    conflicts = []
    for (g, a), entries in by_artifact.items():
        versions = sorted({e["version"] for e in entries if e["version"]})
        if len(versions) > 1 or len(entries) > 1:
            conflicts.append({"type": "VERSION_CONFLICT" if len(versions) > 1 else "DUPLICATE_JAR", "artifact": f"{g + ':' if g else ''}{a}", "versionsFound": versions, "files": [e["file"] for e in entries]})
    return conflicts


def classify_libraries(libs, rules):
    out = []
    for lib in libs:
        entry = {"groupId": lib["groupId"], "artifactId": lib["artifactId"], "version": lib["version"], "file": lib["file"], "detectedFrom": lib["source"], "status": "OK"}
        vt = version_tuple(lib["version"])
        for rule in rules["libraries"]:
            if lib["artifactId"] not in rule["artifactIds"] or (rule["groupId"] and lib["groupId"] and rule["groupId"] != lib["groupId"]): continue
            if rule.get("eol"): entry["status"] = "END_OF_LIFE"
            elif vt and "minSupportedMajor" in rule:
                need = (rule["minSupportedMajor"], rule.get("minSupportedMinor", 0)); padded = vt[:2] + (0,) * (2 - len(vt[:2]))
                if padded < need: entry["status"] = "OUTDATED"
            if entry["status"] != "OK":
                entry["recommendedVersion"] = rule["recommendedVersion"]; entry["notes"] = rule["note"]
                if rule.get("jakarta"): entry["requiresJakarta"] = True
            break
        for eol in rules["eolArtifacts"]:
            if lib["artifactId"] == eol["artifactId"] and (not lib["groupId"] or lib["groupId"] == eol["groupId"]): entry["status"] = "END_OF_LIFE"; entry["notes"] = eol["note"]
        out.append(entry)
    return out


def declared_vs_present(declared, libs):
    present = {(l["groupId"], l["artifactId"]) for l in libs} | {(None, l["artifactId"]) for l in libs}; missing = []
    for d in declared:
        if d.get("scope") in ("provided", "test", "system", "compileOnly", "testImplementation", "testCompile"): continue
        if (d["groupId"], d["artifactId"]) in present or (None, d["artifactId"]) in present: continue
        missing.append({"artifact": f"{d['groupId']}:{d['artifactId']}" + (f":{d['version']}" if d.get("version") else ""), "scope": d.get("scope"), "reason": "Declared in build file but not found in the packaged libraries"})
    return missing
