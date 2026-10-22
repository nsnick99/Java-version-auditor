from __future__ import annotations
import re
import xml.etree.ElementTree as ET
from pathlib import Path

BUILD = {"pom.xml":"Maven","build.gradle":"Gradle","build.gradle.kts":"Gradle","settings.gradle":"Gradle","settings.gradle.kts":"Gradle","build.xml":"Ant"}
SKIP={".git","target","build","out","bin","node_modules",".gradle","__pycache__",".venv","venv"}

def clean(v):
    if not v: return None
    v=v.strip().strip("'\"")
    m=re.fullmatch(r"(?:JavaVersion\.)?VERSION_(?:1_)?(\d+)",v,re.I)
    return m.group(1) if m else v if not v.startswith("${") else None

def java_major(v):
    v=clean(v)
    if not v:return None
    return v.split('.')[1] if v.startswith('1.') else (re.match(r'\d+',v).group(0) if re.match(r'\d+',v) else v)

def tag(root,name):
    return next((x for x in root if x.tag.split('}')[-1]==name),None)

def text(root,name):
    x=tag(root,name); return (x.text or '').strip() if x is not None and x.text else None

def maven(p):
    j=[];b=[];dependencies=[]
    try:r=ET.parse(p).getroot()
    except Exception:return j,b,dependencies

    props=tag(r,'properties')
    if props is not None:
        for x in props:
            if x.text and x.tag.split('}')[-1] in {'java.version','maven.compiler.release','maven.compiler.source','maven.compiler.target','jdk.version'}:
                j.append((java_major(x.text),f'{p.name} > properties.{x.tag.split("}")[-1]}'))
            if x.text and 'spring' in x.tag.lower() and 'boot' in x.tag.lower() and 'version' in x.tag.lower():
                b.append((clean(x.text),f'{p.name} > properties.{x.tag.split("}")[-1]}'))

    parent=tag(r,'parent')
    if parent is not None and text(parent,'groupId')=='org.springframework.boot' and text(parent,'artifactId')=='spring-boot-starter-parent':
        b.append((clean(text(parent,'version')),f'{p.name} > parent.version'))

    for x in r.iter():
        name=x.tag.split('}')[-1]
        if name=='dependency':
            group=text(x,'groupId'); artifact=text(x,'artifactId'); version=text(x,'version')
            if group and artifact:
                scope=text(x,'scope') or 'compile'
                optional=(text(x,'optional') or '').lower()=='true'
                dependencies.append({
                    'group_id':group,
                    'artifact_id':artifact,
                    'version':clean(version),
                    'version_source':'declared' if version else 'managed_or_transitive',
                    'scope':scope,
                    'optional':optional,
                    'source':p.name
                })
            if group=='org.springframework.boot' and artifact=='spring-boot-dependencies':
                b.append((clean(version),f'{p.name} > spring-boot-dependencies.version'))
        if name=='plugin':
            if text(x,'groupId')=='org.springframework.boot' and text(x,'artifactId')=='spring-boot-maven-plugin': b.append((clean(text(x,'version')),f'{p.name} > spring-boot-maven-plugin.version'))

    return j,b,dependencies

def gradle(p,root):
    t=p.read_text(encoding='utf-8',errors='replace');j=[];b=[];dependencies=[]
    for pat in [r'(?:sourceCompatibility|targetCompatibility)\s*=\s*[\'\"](1\.\d+|\d+(?:\.\d+)*)',r'JavaLanguageVersion\.of\(\s*[\'\"]?(\d+)[\'\"]?\s*\)',r'VERSION_(?:1_)?(\d+)\b']:
        j += [(java_major(m.group(1)),f'{p.relative_to(root)} > Java configuration') for m in re.finditer(pat,t,re.I)]
    for pat in [r'org\.springframework\.boot[\'\"]?\s*\)?\s*version\s*[\'\"]([^\'\"]+)',r'springBootVersion\s*=\s*[\'\"]([^\'\"]+)']:
        b += [(clean(m.group(1)),f'{p.relative_to(root)} > Spring Boot configuration') for m in re.finditer(pat,t,re.I)]
    for parent in [p.parent,*p.parents]:
        c=parent/'gradle'/'libs.versions.toml'
        if c.exists():
            t=c.read_text(encoding='utf-8',errors='replace')
            b += [(m.group(1),f'{c.relative_to(root)} > versions.springBoot') for m in re.finditer(r'springBoot\s*=\s*[\"\']([^\"\']+)',t)]
            break
    # Basic Gradle dependency inventory. Dynamic/version-catalog references remain unresolved.
    for m in re.finditer(r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor)\s*[\(]?\s*[\"']([^:\"']+):([^:\"']+)(?::([^\"']+))?[\"']",t):
        dependencies.append({
            'group_id':m.group(1),
            'artifact_id':m.group(2),
            'version':clean(m.group(3)),
            'version_source':'declared' if m.group(3) else 'managed_or_catalog_or_transitive',
            'scope':'gradle',
            'optional':False,
            'source':str(p.relative_to(root))
        })
    return j,b,dependencies

def ant(p):
    j=[];b=[];dependencies=[]; props=p.with_name('build.properties')
    if not props.exists():return j,b,dependencies
    for line in props.read_text(encoding='utf-8',errors='replace').splitlines():
        if '=' not in line:continue
        k,v=line.split('=',1);k=k.strip();v=v.strip()
        if k in {'java.version','javaVersion','jdk.version'}:j.append((java_major(v),f'{props.name} > {k}'))
        if k in {'spring-boot.version','spring.boot.version','springBootVersion'}:b.append((clean(v),f'{props.name} > {k}'))
    return j,b,dependencies

def finding(items):
    items=[x for x in items if x[0]];vs=sorted({x[0] for x in items})
    if not vs:return {'status':'NOT_DETECTED','version':None,'evidence':[]}
    ev=[{'version':v,'source':s} for v,s in items]
    if len(vs)>1:return {'status':'CONFLICT','version':None,'candidates':vs,'evidence':ev}
    return {'status':'DETECTED','version':vs[0],'evidence':ev}

def audit(project):
    root=Path(project).resolve(); groups={}
    for p in root.rglob('*'):
        if p.is_file() and p.name in BUILD and not any(x in SKIP for x in p.parts):groups.setdefault(p.parent,[]).append(p)
    modules=[]
    for folder,files in sorted(groups.items()):
        j=[];b=[];dependencies=[]
        for p in files:
            if p.name=='pom.xml':x,y,d=maven(p)
            elif p.name.startswith(('build.gradle','settings.gradle')):x,y,d=gradle(p,root)
            else:x,y,d=ant(p)
            j+=x;b+=y;dependencies+=d
        modules.append({
            'name':folder.name,
            'path':str(folder.relative_to(root) or '.'),
            'build_systems':sorted({BUILD[p.name] for p in files}),
            'build_files':[str(p.relative_to(root)) for p in files],
            'java':finding(j),
            'spring_boot':finding(b),
            'dependencies':dependencies
        })
    systems=sorted({s for m in modules for s in m['build_systems']});incs=[]
    for key,label in [('java','Java'),('spring_boot','Spring Boot')]:
        vs=sorted({m[key]['version'] for m in modules if m[key]['status']=='DETECTED'})
        if len(vs)>1:incs.append({'technology':label,'type':'VERSION_VARIATION_ACROSS_MODULES','versions':vs})

    all_dependencies=[]
    for m in modules:
        all_dependencies.extend(m['dependencies'])
    dependency_summary={}
    for d in all_dependencies:
        key=f"{d['group_id']}:{d['artifact_id']}"
        dependency_summary.setdefault(key,{'versions':set(),'modules':set(),'version_sources':set()})
        if d['version']:dependency_summary[key]['versions'].add(d['version'])
        dependency_summary[key]['modules'].add(next((m['name'] for m in modules if d in m['dependencies']),''))
        dependency_summary[key]['version_sources'].add(d['version_source'])
    dep_conflicts=[]
    for key,v in dependency_summary.items():
        versions=sorted(v['versions'])
        if len(versions)>1:
            dep_conflicts.append({'dependency':key,'versions':versions})

    return {
        'project':root.name,
        'build_system':'Unknown' if not systems else systems[0] if len(systems)==1 else 'Multiple',
        'detected_build_systems':systems,
        'module_count':len(modules),
        'modules':modules,
        'dependency_count':len(all_dependencies),
        'dependency_version_conflicts':dep_conflicts,
        'version_inconsistencies':incs
    }


def cli():
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Audit a Java project for build systems, Java versions, Spring Boot versions, and declared dependencies."
    )
    parser.add_argument("project", help="Path to the Java project directory")
    parser.add_argument(
        "-o", "--output",
        help="Optional path for the JSON report (prints JSON to the terminal when omitted)"
    )
    args = parser.parse_args()

    project = Path(args.project).expanduser()
    if not project.exists():
        parser.error(f"Project path does not exist: {project}")
    if not project.is_dir():
        parser.error(f"Project path must be a directory: {project}")

    report = audit(project)
    rendered = json.dumps(report, indent=2)

    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Audit report written to {output}")
    else:
        print(rendered)


if __name__ == "__main__":
    cli()
