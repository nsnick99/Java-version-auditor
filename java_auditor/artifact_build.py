"""Build type, packaging, Java version and application version detection."""

import re
import xml.etree.ElementTree as ET
from collections import Counter
from .classfile import major_to_java, parse_class, ClassFormatError


def parse_manifest(text):
    result, last = {}, None
    for line in text.splitlines():
        if line.startswith(" ") and last: result[last] += line[1:]
        elif ":" in line:
            key, _, val = line.partition(":"); last = key.strip(); result[last] = val.strip()
    return result


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag: el.tag = el.tag.split("}", 1)[1]
    return root


def parse_pom(text):
    try: root = _strip_ns(ET.fromstring(text))
    except ET.ParseError: return None
    def find(path):
        el = root.find(path); return el.text.strip() if el is not None and el.text else None
    props = {}
    pe = root.find("properties")
    if pe is not None:
        for child in pe: props[child.tag] = (child.text or "").strip()
    def resolve(value):
        if value is None: return None
        m = re.fullmatch(r"\$\{(.+)\}", value); return props.get(m.group(1), value) if m else value
    deps = []
    for d in root.findall("./dependencies/dependency"):
        deps.append({"groupId": d.findtext("groupId", "").strip(), "artifactId": d.findtext("artifactId", "").strip(), "version": resolve((d.findtext("version") or "").strip() or None), "scope": (d.findtext("scope") or "compile").strip()})
    parent = root.find("parent"); parent_info = None
    if parent is not None: parent_info = {"groupId": parent.findtext("groupId", "").strip(), "artifactId": parent.findtext("artifactId", "").strip(), "version": parent.findtext("version", "").strip()}
    compiler_cfg = {}
    for plugin in root.findall(".//plugin"):
        if plugin.findtext("artifactId") == "maven-compiler-plugin":
            cfg = plugin.find("configuration")
            if cfg is not None:
                for key in ("source", "target", "release"):
                    val = cfg.findtext(key)
                    if val: compiler_cfg[key] = resolve(val.strip())
    return {"groupId": find("groupId") or (parent_info or {}).get("groupId"), "artifactId": find("artifactId"), "version": find("version") or (parent_info or {}).get("version"), "packaging": find("packaging") or "jar", "name": find("name"), "parent": parent_info, "properties": props, "compiler": compiler_cfg, "dependencies": deps}


GRADLE_DEP = re.compile(r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|compile|runtime|testCompile|providedCompile)\s*\(?\s*['\"](?P<g>[^:'\"]+):(?P<a>[^:'\"]+)(?::(?P<v>[^'\"]+))?['\"]", re.X)


def parse_gradle(text):
    deps = [{"groupId": m.group("g"), "artifactId": m.group("a"), "version": m.group("v"), "scope": "implementation"} for m in GRADLE_DEP.finditer(text)]
    compat = {}
    for key, pat in (("source", r"sourceCompatibility\s*=\s*['\"]?([\w.]+)"), ("target", r"targetCompatibility\s*=\s*['\"]?([\w.]+)"), ("toolchain", r"languageVersion\s*(?:=|\.set\()\s*JavaLanguageVersion\.of\((\d+)\)"), ("jvmTarget", r"jvmTarget\s*=\s*['\"]?([\w.]+)")):
        m = re.search(pat, text)
        if m: compat[key] = m.group(1)
    return {"dependencies": deps, "compatibility": compat, "plugins": []}


def detect_build(src):
    names = src.names(); info = {"tool": "UNKNOWN", "toolVersion": None, "packaging": None, "detectedFrom": [], "declaredFiles": []}; pom = gradle = None
    def shallowest(suffix):
        hits = sorted((n for n in names if n.lower().endswith(suffix)), key=lambda x: x.count("/")); return hits[0] if hits else None
    pom_path = shallowest("pom.xml"); gradle_path = shallowest("build.gradle") or shallowest("build.gradle.kts")
    ant_path = shallowest("build.xml")
    if pom_path: info["tool"] = "Maven"; info["detectedFrom"].append(pom_path); pom = parse_pom(src.read_text(pom_path))
    elif gradle_path: info["tool"] = "Gradle"; info["detectedFrom"].append(gradle_path); gradle = parse_gradle(src.read_text(gradle_path))
    elif ant_path: info["tool"] = "Ant"; info["detectedFrom"].append(ant_path)
    embedded = [n for n in names if n.startswith("META-INF/maven/") and n.endswith("/pom.xml")]
    if info["tool"] == "UNKNOWN" and embedded: info["tool"] = "Maven"; info["detectedFrom"].append(embedded[0]); pom = parse_pom(src.read_text(embedded[0]))
    manifest = parse_manifest(src.read_text("META-INF/MANIFEST.MF")) if "META-INF/MANIFEST.MF" in names else {}
    created_by = manifest.get("Created-By", "")
    if info["tool"] == "UNKNOWN":
        if "Maven" in created_by: info["tool"] = "Maven"
        elif "Gradle" in created_by: info["tool"] = "Gradle"
        elif "Ant-Version" in manifest: info["tool"] = "Ant"
    if pom: info["packaging"] = pom["packaging"]
    elif src.kind in ("JAR", "WAR", "EAR"): info["packaging"] = src.kind.lower()
    if any(n.startswith("BOOT-INF/") for n in names): info["springBootFatJar"] = True
    if any(n.startswith("WEB-INF/") for n in names) and info["packaging"] in (None, "jar"): info["packaging"] = "war"
    info["buildJdk"] = manifest.get("Build-Jdk-Spec") or manifest.get("Build-Jdk")
    info["createdBy"] = created_by or None; info["builtBy"] = manifest.get("Built-By")
    return info, pom, gradle, manifest


def detect_java(src, pom, gradle, class_infos):
    declared = {}
    if pom:
        comp = pom["compiler"]; props = pom["properties"]
        for key, prop in (("source", "maven.compiler.source"), ("target", "maven.compiler.target"), ("release", "maven.compiler.release")):
            val = comp.get(key) or props.get(prop)
            if val: declared[key] = val
        if "java.version" in props: declared["javaVersionProperty"] = props["java.version"]
    if gradle: declared.update(gradle["compatibility"])
    majors = Counter(ci.major for ci in class_infos if ci.major); result = {"compiledMajorVersion": None, "compiledAs": None, "declared": declared or None, "mixedClassVersions": len(majors) > 1, "classVersionHistogram": None}
    if majors:
        top = max(majors); result["compiledMajorVersion"] = top; result["compiledAs"] = f"Java {major_to_java(top)}"; result["classVersionHistogram"] = {f"Java {major_to_java(k)}": v for k, v in sorted(majors.items())}
    return result


def detect_app_version(manifest, pom, src):
    for key in ("Implementation-Version", "Bundle-Version", "Specification-Version"):
        if manifest.get(key): return manifest[key]
    return pom.get("version") if pom else None


def detect_app_name(manifest, pom, src_label):
    if pom and pom.get("artifactId"): return pom["artifactId"]
    for key in ("Implementation-Title", "Bundle-Name", "Bundle-SymbolicName"):
        if manifest.get(key): return manifest[key]
    base = src_label.rstrip("/").split("/")[-1]; return re.sub(r"[-_.]?\d.*$", "", base.rsplit(".", 1)[0]) or base


def collect_classes(src, prefixes=("WEB-INF/classes/", "BOOT-INF/classes/", "")):
    infos = []
    for name in src.names():
        if not name.endswith(".class") or name.endswith("module-info.class") or name.startswith("META-INF/versions/"): continue
        try: infos.append((name, parse_class(src.read(name))))
        except (ClassFormatError, Exception): continue
    return infos
