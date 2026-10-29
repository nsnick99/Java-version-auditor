from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = "Java-Version-Auditor/2.0"
HTTP_TIMEOUT = 8
SKIP_DIRS = {".git", "target", "build", "out", "bin", "node_modules", ".gradle", "__pycache__", ".venv", "venv"}
BUILD_FILES = {"pom.xml": "Maven", "build.gradle": "Gradle", "build.gradle.kts": "Gradle", "build.xml": "Ant"}

FRAMEWORK_RULES = [
    ("Spring Boot", lambda g, a: g == "org.springframework.boot" and (a == "spring-boot-starter-parent" or a.startswith("spring-boot-"))),
    ("Spring MVC", lambda g, a: g == "org.springframework" and a in {"spring-webmvc", "spring-web"}),
    ("Spring Security", lambda g, a: g == "org.springframework.security" or a.startswith("spring-security-")),
    ("Hibernate", lambda g, a: g in {"org.hibernate", "org.hibernate.orm"} and a.startswith("hibernate-")),
    ("Struts", lambda g, a: g.startswith("org.apache.struts") or a.startswith("struts2-")),
    ("JSF", lambda g, a: g in {"javax.faces", "jakarta.faces"} or a in {"jsf-api", "jakarta.faces-api"}),
    ("Apache Camel", lambda g, a: g == "org.apache.camel" or a.startswith("camel-")),
    ("JPA", lambda g, a: g in {"javax.persistence", "jakarta.persistence"} or a in {"javax.persistence-api", "jakarta.persistence-api"}),
]

DEPRECATED_API_RULES = [
    (r"\bsun\.misc\.", "sun.misc API", "Internal JDK API; avoid direct use on modern JDKs."),
    (r"\bjavax\.xml\.bind\.", "JAXB API", "JAXB was removed from the JDK after Java 8; use the supported JAXB API/runtime dependency when required."),
    (r"\bSecurityManager\b", "SecurityManager", "SecurityManager is deprecated for removal in modern Java releases."),
    (r"\bjava\.lang\.ThreadGroup\b", "ThreadGroup", "Review ThreadGroup usage for modern-JDK compatibility."),
    (r"\bjava\.util\.Observable\b|\bjava\.util\.Observer\b", "Observable/Observer", "Deprecated Java API; replace with a modern event mechanism."),
]

LIFECYCLE_PRODUCTS = {"java": "java", "spring-boot": "spring-boot", "spring-framework": "spring-framework", "spring-security": "spring-security", "hibernate": "hibernate", "tomcat": "tomcat"}


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(v):
    if v is None: return None
    v = str(v).strip().strip("'\"")
    if not v or "${" in v: return None
    m = re.fullmatch(r"(?:JavaVersion\.)?VERSION_(?:1_)?(\d+)", v, re.I)
    return m.group(1) if m else v


def java_major(v):
    v = clean(v)
    if not v: return None
    if v.startswith("1."): return v.split(".")[1]
    m = re.match(r"\d+", v)
    return m.group(0) if m else v


def version_tuple(v):
    m = re.match(r"^\s*[vV]?(\d+(?:\.\d+)*)", str(v or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def compare_versions(a, b):
    x, y = version_tuple(a), version_tuple(b)
    if not x or not y: return 0
    n = max(len(x), len(y)); x += (0,) * (n-len(x)); y += (0,) * (n-len(y))
    return (x > y) - (x < y)


def http_json(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data: headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r: return json.loads(r.read().decode("utf-8", errors="replace"))


def http_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r: return r.read().decode("utf-8", errors="replace")


def maven_latest(group, artifact):
    url = f"https://repo.maven.apache.org/maven2/{'/'.join(group.split('.'))}/{urllib.parse.quote(artifact)}/maven-metadata.xml"
    try:
        root = ET.fromstring(http_text(url))
        versioning = next((x for x in root if x.tag.split('}')[-1] == 'versioning'), None)
        release = next((x.text for x in list(versioning or []) if x.tag.split('}')[-1] == 'release' and x.text), None)
        if release and not re.search(r"(?i)(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea)", release): return {"status":"FOUND","version":release,"source":url}
        vn = next((x for x in list(versioning or []) if x.tag.split('}')[-1] == 'versions'), None)
        versions = [x.text.strip() for x in list(vn or []) if x.text and not re.search(r"(?i)(alpha|beta|rc|milestone|snapshot|preview|nightly|dev|ea)", x.text)]
        latest = max(versions, key=version_tuple) if versions else None
        return {"status":"FOUND" if latest else "NO_STABLE_RELEASE","version":latest,"source":url}
    except urllib.error.HTTPError as e: return {"status":"NOT_FOUND" if e.code == 404 else "UNAVAILABLE","version":None,"source":url}
    except Exception as e: return {"status":"UNAVAILABLE","version":None,"message":str(e)[:200],"source":url}


def osv_scan(dependencies):
    queries=[{"package":{"ecosystem":"Maven","name":f"{d['group_id']}:{d['artifact_id']}"},"version":d["version"]} for d in dependencies if d.get("version")]
    if not queries: return [], {"status":"NO_VERSIONED_DEPENDENCIES"}
    try:
        data=http_json("https://api.osv.dev/v1/querybatch",{"queries":queries}); findings=[]
        versioned=[d for d in dependencies if d.get("version")]
        for dep,result in zip(versioned,data.get("results",[])):
            for v in result.get("vulns",[]) or []:
                findings.append({"dependency":f"{dep['group_id']}:{dep['artifact_id']}","version":dep["version"],"id":v.get("id"),"summary":v.get("summary"),"aliases":v.get("aliases",[]),"severity":(v.get("database_specific") or {}).get("severity")})
        return findings,{"status":"COMPLETE","queried":len(queries)}
    except Exception as e: return [],{"status":"UNAVAILABLE","message":str(e)[:250]}


def lifecycle(product, version):
    slug=LIFECYCLE_PRODUCTS.get(product)
    if not slug or not version: return {"status":"NOT_CATALOGUED"}
    url=f"https://endoflife.date/api/{slug}.json"
    try:
        rows=http_json(url); cycle=java_major(version) if product=="java" else ".".join(version.split(".")[:2])
        row=next((x for x in rows if str(x.get("cycle"))==str(cycle)),None)
        if not row: return {"status":"CYCLE_NOT_FOUND","product":product,"cycle":cycle}
        eol=row.get("eol"); today=datetime.now(timezone.utc).date().isoformat()
        status="EOL" if eol is True or (isinstance(eol,str) and eol and eol <= today) else "SUPPORTED_OR_CURRENT"
        return {"status":status,"product":product,"cycle":cycle,"eol":eol,"support":row.get("support"),"release_date":row.get("releaseDate"),"latest":row.get("latest"),"source":url}
    except urllib.error.HTTPError as e: return {"status":"NOT_CATALOGUED" if e.code==404 else "UNAVAILABLE","product":product}
    except Exception as e: return {"status":"UNAVAILABLE","product":product,"message":str(e)[:200]}


def interpolate(v, props):
    if not v: return None
    for _ in range(5):
        old=v; v=re.sub(r"\$\{([^}]+)\}",lambda m:props.get(m.group(1),m.group(0)),v)
        if v==old: break
    return clean(v)


def xml_text(node,name):
    if node is None:return None
    x=next((c for c in node if c.tag.split('}')[-1]==name),None)
    return (x.text or '').strip() if x is not None and x.text else None


def parse_pom(path):
    try: root=ET.parse(path).getroot()
    except Exception as e:return {"error":str(e),"java":[],"spring_boot":[],"dependencies":[]}
    props={}; pn=next((x for x in root if x.tag.split('}')[-1]=='properties'),None)
    for x in list(pn or []):
        if x.text: props[x.tag.split('}')[-1]]=x.text.strip()
    java=[]
    for key in ("java.version","maven.compiler.release","maven.compiler.source","maven.compiler.target"):
        if key in props: java.append({"version":java_major(interpolate(props[key],props)),"source":f"{path.name}:properties.{key}"})
    parent=next((x for x in root if x.tag.split('}')[-1]=='parent'),None); boot=[]
    if parent is not None and xml_text(parent,'groupId')=='org.springframework.boot' and xml_text(parent,'artifactId')=='spring-boot-starter-parent': boot.append({"version":interpolate(xml_text(parent,'version'),props),"source":f"{path.name}:parent.version"})
    managed={}; dm=next((x for x in root if x.tag.split('}')[-1]=='dependencyManagement'),None); dn=next((x for x in list(dm or []) if x.tag.split('}')[-1]=='dependencies'),None)
    for d in list(dn or []):
        if d.tag.split('}')[-1]!='dependency':continue
        g,a,v=xml_text(d,'groupId'),xml_text(d,'artifactId'),xml_text(d,'version')
        if g and a and v:managed[(g,a)]=interpolate(v,props)
    deps=[]; dn=next((x for x in root if x.tag.split('}')[-1]=='dependencies'),None)
    for d in list(dn or []):
        if d.tag.split('}')[-1]!='dependency':continue
        g,a=xml_text(d,'groupId'),xml_text(d,'artifactId')
        if not g or not a:continue
        raw=xml_text(d,'version'); v=interpolate(raw,props) if raw else managed.get((g,a))
        deps.append({"group_id":g,"artifact_id":a,"version":v,"version_source":"direct" if raw else ("dependencyManagement" if v else "managed_or_transitive"),"scope":xml_text(d,'scope') or 'compile',"optional":(xml_text(d,'optional') or 'false').lower()=='true',"source":str(path)})
    return {"java":java,"spring_boot":boot,"dependencies":deps,"error":None}


def parse_gradle(path):
    t=path.read_text(encoding='utf-8',errors='replace'); java=[]; boot=[]; deps=[]
    for m in re.finditer(r"(?:sourceCompatibility|targetCompatibility)\s*[=:]\s*['\"]?(?:JavaVersion\.)?VERSION_(?:1_)?(\d+)|JavaLanguageVersion\.of\(\s*(\d+)\s*\)",t,re.I):java.append({"version":m.group(1) or m.group(2),"source":f"{path.name}:Java configuration"})
    for m in re.finditer(r"(?:org\.springframework\.boot:spring-boot-gradle-plugin:|id\s+['\"]org\.springframework\.boot['\"]\s+version\s+['\"])([^'\"]+)",t):boot.append({"version":m.group(1),"source":f"{path.name}:Spring Boot plugin"})
    for m in re.finditer(r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|annotationProcessor)\s*\(?\s*['\"]([^:'\"]+):([^:'\"]+):([^'\"]+)['\"]",t):deps.append({"group_id":m.group(1),"artifact_id":m.group(2),"version":clean(m.group(3)),"version_source":"direct","scope":"gradle","optional":False,"source":str(path)})
    return {"java":java,"spring_boot":boot,"dependencies":deps,"error":None}


def identify_frameworks(deps):
    found={}
    for d in deps:
        for name,rule in FRAMEWORK_RULES:
            if rule(d['group_id'],d['artifact_id']):found.setdefault(name,[]).append({"group_id":d['group_id'],"artifact_id":d['artifact_id'],"version":d.get('version'),"source":d.get('source')})
    return [{"name":k,"components":v} for k,v in sorted(found.items())]


def version_finding(items):
    vals=sorted({x.get('version') for x in items if x.get('version')})
    if not vals:return {"status":"NOT_DETECTED","version":None,"evidence":[]}
    return {"status":"CONFLICT","version":None,"versions":vals,"evidence":items} if len(vals)>1 else {"status":"DETECTED","version":vals[0],"evidence":items}


def dependency_audit(deps,online=True):
    out=[]
    for d in deps:
        item=dict(d); item.update({'latest_version':None,'update_available':None,'version_status':'UNKNOWN','latest_check':{'status':'SKIPPED'}})
        if online and d.get('version'):
            latest=maven_latest(d['group_id'],d['artifact_id']); item['latest_check']=latest
            if latest.get('version'):
                item['latest_version']=latest['version']; c=compare_versions(d['version'],latest['version']); item['update_available']=c<0; item['version_status']='OUTDATED' if c<0 else ('CURRENT' if c==0 else 'NEWER_THAN_REPOSITORY')
        out.append(item)
    return out


def scan_deprecated_apis(root):
    findings=[]
    for p in root.rglob('*.java'):
        if any(x in SKIP_DIRS for x in p.parts):continue
        text=p.read_text(encoding='utf-8',errors='replace')
        for pattern,name,reason in DEPRECATED_API_RULES:
            for m in re.finditer(pattern,text):
                line=text.count('\n',0,m.start())+1; evidence=text.splitlines()[line-1].strip()[:240]
                findings.append({'file':str(p.relative_to(root)),'line':line,'api':name,'reason':reason,'evidence':evidence})
    return findings


def build_validation(root,systems):
    if 'Maven' in systems: cmd=[str(root/'mvnw'),'-q','-DskipTests','package'] if (root/'mvnw').exists() else ['mvn','-q','-DskipTests','package']
    elif 'Gradle' in systems: cmd=[str(root/'gradlew'),'build','-x','test'] if (root/'gradlew').exists() else ['gradle','build','-x','test']
    else:return {'status':'NOT_RUN','reason':'No supported build system'}
    try:
        p=subprocess.run(cmd,cwd=root,text=True,capture_output=True,timeout=300)
        return {'status':'PASS' if p.returncode==0 else 'FAIL','command':' '.join(cmd),'exit_code':p.returncode,'stdout_tail':p.stdout[-3000:],'stderr_tail':p.stderr[-3000:]}
    except Exception as e:return {'status':'ERROR','command':' '.join(cmd),'message':str(e)}


def runtime_check(url):
    try:
        req=urllib.request.Request(url,headers={'User-Agent':USER_AGENT})
        with urllib.request.urlopen(req,timeout=HTTP_TIMEOUT) as r:return {'status':'PASS','url':url,'http_status':r.status}
    except urllib.error.HTTPError as e:return {'status':'FAIL','url':url,'http_status':e.code}
    except Exception as e:return {'status':'UNAVAILABLE','url':url,'message':str(e)[:250]}


def audit(project,online=True,validate=False,runtime_url=None,target_java=None):
    root=Path(project).expanduser().resolve()
    if not root.is_dir():raise ValueError('Project path must be an existing directory')
    build_files=[p for p in root.rglob('*') if p.is_file() and p.name in BUILD_FILES and not any(x in SKIP_DIRS for x in p.parts)]
    modules=[]; all_deps=[]; java_evidence=[]; boot_evidence=[]
    for p in sorted(build_files):
        parsed=parse_pom(p) if p.name=='pom.xml' else parse_gradle(p); java_evidence+=parsed['java']; boot_evidence+=parsed['spring_boot']; all_deps+=parsed['dependencies']
        modules.append({'path':str(p.parent.relative_to(root) or '.'),'build_system':BUILD_FILES[p.name],'build_file':str(p.relative_to(root)),'java':version_finding(parsed['java']),'spring_boot':version_finding(parsed['spring_boot']),'dependency_count':len(parsed['dependencies']),'parse_error':parsed.get('error')})
    systems=sorted({m['build_system'] for m in modules}); deps=dependency_audit(all_deps,online); frameworks=identify_frameworks(deps)
    vulnerabilities,vuln_status=osv_scan(deps) if online else ([],{'status':'SKIPPED_OFFLINE'}); deprecated=scan_deprecated_apis(root); java=version_finding(java_evidence); boot=version_finding(boot_evidence)
    compatibility=[]
    if target_java and java.get('version'):
        try:
            if int(java['version'])>target_java:compatibility.append({'type':'TARGET_JAVA_LOWER_THAN_PROJECT','current':java['version'],'target':target_java,'severity':'HIGH'})
            elif int(java['version'])<target_java:compatibility.append({'type':'TARGET_JAVA_HIGHER_THAN_PROJECT','current':java['version'],'target':target_java,'severity':'INFO'})
        except ValueError:pass
    if boot.get('version') and java.get('version'):
        b=version_tuple(boot['version']); j=int(java['version'])
        if b and b[0]>=3 and j<17:compatibility.append({'type':'SPRING_BOOT_JAVA_COMPATIBILITY','spring_boot':boot['version'],'java':java['version'],'severity':'HIGH','rule':'Spring Boot 3.x requires Java 17+'})
    lifecycle_checks={'java':lifecycle('java',java['version']) if java.get('version') and online else {'status':'SKIPPED_OFFLINE'},'spring_boot':lifecycle('spring-boot',boot['version']) if boot.get('version') and online else {'status':'SKIPPED_OFFLINE'}}
    return {'schema_version':'2.0','generated_at':now_utc(),'project':root.name,'build_system':systems[0] if len(systems)==1 else ('Multiple' if systems else 'Unknown'),'detected_build_systems':systems,'modules':modules,'java':java,'spring_boot':boot,'frameworks':frameworks,'dependencies':deps,'dependency_count':len(deps),'vulnerabilities':vulnerabilities,'deprecated_apis':deprecated,'compatibility_checks':compatibility,'lifecycle':lifecycle_checks,'build_validation':build_validation(root,systems) if validate else {'status':'NOT_RUN'},'runtime_check':runtime_check(runtime_url) if runtime_url else {'status':'NOT_RUN'},'checks':{'latest_versions':'enabled' if online else 'disabled','osv_vulnerabilities':vuln_status,'deprecated_api_scan':'complete','build_validation':'complete' if validate else 'not_run','runtime_check':'complete' if runtime_url else 'not_run'},'notes':['Technology/version audit only. No migration readiness score or migration plan is generated.']}


def main():
    ap=argparse.ArgumentParser(description='Audit Java projects for versions, frameworks, dependencies, compatibility, vulnerabilities and deprecated APIs.')
    ap.add_argument('project'); ap.add_argument('-o','--output'); ap.add_argument('--offline',action='store_true'); ap.add_argument('--validate-build',action='store_true'); ap.add_argument('--runtime-url'); ap.add_argument('--target-java',type=int)
    args=ap.parse_args()
    try:report=audit(args.project,online=not args.offline,validate=args.validate_build,runtime_url=args.runtime_url,target_java=args.target_java)
    except Exception as e:ap.error(str(e))
    rendered=json.dumps(report,indent=2)
    if args.output:Path(args.output).expanduser().write_text(rendered+'\n',encoding='utf-8');print(f'Audit report written to {args.output}')
    else:print(rendered)

if __name__=='__main__':main()
