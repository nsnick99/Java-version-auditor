# Java Version Auditor

Scans a Java repository and writes one clean JSON report with Java, framework, and dependency versions, missing referenced files, compatibility findings, and upgrade recommendations.

## Run

Run one command from the Java Version Auditor directory:

```bash
python auditor.py /path/to/java-project
```

On Windows, for example:

```powershell
python auditor.py "C:\Users\YourName\Desktop\Java-test"
```

The script creates `output.json` in the current working directory. The intended Java target is configured once at the top of `auditor.py` with `TARGET_JAVA_VERSION = "21"`; edit that value to change the target. There are no optional CLI flags.

## Report structure

The JSON is organized around:

- `project_name`, `build_system`, `current_java`, and `target_java`
- `modules`: module path, build file, build system, and detected Java version
- `frameworks`: detected framework, actual version, module/source evidence, latest available version, recommended version, and version status
- `dependencies`: direct and resolved transitive dependency coordinates, versions, scope, source, and update recommendation
- `missing_files`: missing module directories/POMs, explicitly referenced parent POMs, missing system-path dependencies, missing configured resource directories, and missing Gradle subprojects
- `issues`: relevant unresolved versions, Java/framework compatibility findings, missing references, and selected deprecated Java API usages
- `recommendations`: current version to recommended version, with a concise reason

## Framework version resolution

The scanner reads build declarations and uses Maven's `dependency:tree` or Gradle's `dependencies --configuration runtimeClasspath` report when the relevant tool or wrapper is available. This resolves versions that are inherited through Maven parents, `dependencyManagement`, or a Spring Boot BOM, and exposes transitive framework artifacts such as Spring MVC, Spring Security, Hibernate, and JPA.

If dependency resolution cannot complete, static dependency declarations are retained and the report adds an issue noting that inherited or transitive versions may be missing. Unresolved versions are not guessed.

## Version recommendations

For each resolved dependency, the scanner compares the detected version with stable release metadata from Maven Central. It recommends the newest stable version within the current major line when one is available; when only a newer major line exists, it labels that as a major-version update and recommends reviewing compatibility before upgrading. Framework-specific Java compatibility findings are included where rules are known.

## Intentionally excluded

- Starting the application or checking ports/health endpoints
- Build or test validation
- Vulnerability database scans
- End-of-life/lifecycle scans
- Migration readiness scores or migration plans
- Automatic code changes

No third-party Python packages are required. Maven/Gradle is only invoked to resolve dependency trees; the scanner does not run the application or test suite.
