from __future__ import annotations
import argparse, json
from auditor import audit

p=argparse.ArgumentParser(description='Audit Java build systems and Java/Spring Boot versions.')
p.add_argument('project')
p.add_argument('-o','--output',default='audit-report.json')
a=p.parse_args()
report=audit(a.project)
with open(a.output,'w',encoding='utf-8') as f: json.dump(report,f,indent=2)
print(f"Build system: {report['build_system']}")
print(f"Modules found: {report['module_count']}")
for m in report['modules']:
    print(f"- {m['path']}: Java={m['java']['version'] or m['java']['status']}, Spring Boot={m['spring_boot']['version'] or m['spring_boot']['status']}")
print(f"Report: {a.output}")
