# Java Version Auditor

Standalone analyzer for auditing a Java project's **build system, Java/Spring Boot versions, declared dependencies, and dependency version conflicts per module**.

## Supported build systems
- Maven: `pom.xml`
- Gradle: `build.gradle`, `build.gradle.kts`, `settings.gradle*`
- Ant: `build.xml` + common `build.properties`

## Run

```bash
python main.py /path/to/java-project
```

Or choose the report path:

```bash
python main.py /path/to/java-project --output audit-report.json
```

You can also run the analyzer directly:

```bash
python auditor.py /path/to/java-project
```

## Current analysis

The analyzer currently detects:

- Build system and build files
- Java version with evidence
- Spring Boot version with evidence
- Modules in multi-module projects
- Declared Maven dependencies and their versions
- Basic Gradle dependency declarations and their versions
- Dependencies whose versions are inherited/managed rather than declared directly
- Dependency version conflicts across detected modules
- Java/Spring Boot version variations across modules

For Maven, versions managed by the Spring Boot parent or other dependency management are intentionally reported as `managed_or_transitive` when the dependency itself has no `<version>`.

## Scope

This project is a **Java Version Auditor**, not a migration-readiness analyzer. It does not calculate migration readiness, migration blockers, migration paths, or rewrite effort.

Future analysis can add external version intelligence such as EOL/deprecated-version detection and vulnerability checks while keeping the project focused on Java technology and dependency auditing.
