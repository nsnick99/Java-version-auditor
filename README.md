# Java Version Auditor

A Java project auditor focused on technology/version analysis. It does **not** calculate migration readiness or generate a migration plan.

## Scanner engine

1. Repository/build discovery
2. Java and Spring Boot version detection
3. Dependency inventory and version resolution
4. Framework identification
5. Latest-version intelligence
6. Vulnerability checks through OSV
7. Lifecycle/EOL checks for supported technology families
8. Deprecated JDK API detection
9. Compatibility checks
10. Optional build validation
11. Optional runtime/health check
12. JSON audit report

## Frameworks detected

- Spring Boot
- Spring MVC
- Spring Security
- Hibernate
- Struts
- JSF
- Apache Camel
- JPA

Frameworks are detected from dependency coordinates, not from project names alone.

## Compatibility

Compatibility is reported as an audit finding. Examples:

- Spring Boot 3.x running/configured with Java below 17
- Project Java version compared with an optional target JDK
- Java/Spring Boot version conflicts across modules

Example:

```bash
python auditor.py /path/to/project --target-java 21
```

This does not calculate readiness or perform migration work.

## Deprecated API detection

The Java source scanner checks for common APIs that deserve review on newer JDKs:

- `sun.misc.*`
- `javax.xml.bind.*`
- `SecurityManager`
- `ThreadGroup`
- `java.util.Observable` / `Observer`

Findings contain file, line, API, reason and source evidence.

## Dependency and security audit

Online mode checks versioned Maven dependencies against Maven Central and OSV. The report can show:

- current version
- latest stable version
- current → latest comparison
- outdated/current status
- vulnerability IDs and summaries

A missing online result is reported as unavailable; it is never treated as proof that a dependency is safe.

## Lifecycle

Java and selected framework families are checked against endoflife.date when online. EOL, unsupported/unknown, and unavailable states are kept explicit.

## Optional build validation

```bash
python auditor.py /path/to/project --validate-build
```

Maven/Gradle wrappers are preferred when present. Build validation is never run by default.

## Optional runtime check

If an application is already running:

```bash
python auditor.py /path/to/project --runtime-url http://localhost:8080/actuator/health
```

The analyzer only checks the supplied URL; it does not start the application automatically.

## Offline mode

```bash
python auditor.py /path/to/project --offline
```

Offline mode performs local analysis without external version, lifecycle or vulnerability lookups.

## Scope boundary

This project intentionally does not produce:

- migration readiness scores
- migration blockers
- migration plans
- rewrite/replace effort estimates
- target-framework migration plans
