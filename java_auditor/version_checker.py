"""Published-version lookup and framework update classification."""

from __future__ import annotations

import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .config import FRAMEWORK_RULES, HTTP_TIMEOUT, PRE_RELEASE, USER_AGENT
from .utils import child, compare_versions, node_text, rel, version_sort_key, version_tuple

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
