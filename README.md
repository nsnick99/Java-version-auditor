# Java Version Auditor

Standalone MVP for auditing a Java project's **build system, Java version, and Spring Boot version per module**.

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

## Output

The JSON report contains the detected build system, discovered modules, Java version, Spring Boot version, evidence for each detection, conflicts, and version variations across modules.

## Current scope

This is intentionally separate from the VB.NET analyzer. The MVP does **not** analyze individual dependency versions, source-code structure, or migration blockers. It focuses on build detection and Java/Spring Boot version inventory.

## Direct CLI

You can also run the analyzer directly:

```bash
python auditor.py /path/to/java-project
```

Prints the JSON report to the terminal. To save it to a file:

```bash
python auditor.py /path/to/java-project --output audit-report.json
```

The `main.py` command remains available for a short summary plus a saved JSON report.
