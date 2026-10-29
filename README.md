# Java Version Auditor

A small Java project scanner that creates **one clean JSON report** containing the Java, framework, and dependency versions found in a project.

## Run

```bash
python auditor.py /path/to/java-project
```

That is the only command required. It creates `output.json` in the current directory.

## Report structure

```json
{
  "project_name": "SmallestSpringApp",
  "build_system": "Maven",
  "current_java": "11",
  "frameworks": [
    {"name": "Spring Boot", "version": "2.7.5"},
    {"name": "Spring MVC", "version": "5.3.23"},
    {"name": "Spring Security", "version": "5.7.6"},
    {"name": "Hibernate", "version": "5.6.14.Final"}
  ],
  "dependencies": [
    {"name": "com.mysql:mysql-connector-j", "version": "8.0.33"}
  ]
}
```

## Detects

- Java version
- Maven / Gradle
- Spring Boot
- Spring MVC
- Spring Security
- Hibernate
- Struts
- JSF
- Apache Camel
- JPA
- Declared dependency versions
- Versions inherited from Maven dependency management / Spring Boot BOM when available

For Spring Boot projects, the scanner reads the Spring Boot dependency BOM to resolve framework versions such as Spring Framework, Spring Security, Hibernate, and JPA when those versions are managed rather than written directly in `pom.xml`.

## Intentionally excluded

- Runtime checks
- Build execution
- Vulnerability scans
- EOL/lifecycle scans
- Deprecated API scans
- Migration readiness
- Migration planning
- Readiness scoring
- Optional command-line switches

The goal is simply: **scan the project → identify technologies → identify their versions → create a clean JSON report.**
