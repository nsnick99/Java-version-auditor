# Java Version Auditor

One command audits a Java project directory **or a packaged `.war/.jar/.ear/.zip`** and writes `output.json`. The report focuses on actionable Java-version, dependency, API, packaging and migration findings.

## Run

```bash
python auditor.py /path/to/java-project
python auditor.py /path/to/app.war
python auditor.py /path/to/app.war --target-java 17
```

The default target Java version is **21**. On Windows:

```powershell
python auditor.py "C:\Users\YourName\Desktop\Java-test"
```

The report is written beside `auditor.py`, not inside the scanned project.

### Audit modes

The default mode is **fast**. It uses short network/tool timeouts and skips the Maven Versions Plugin fallback, which is often the slowest part of a large Maven scan. Maven/Gradle dependency-tree resolution is still attempted when the project provides an executable or wrapper.

```powershell
# Fast/default mode
python auditor.py "C:\Projects\MyJavaApp"

# Explicit target Java
python auditor.py "C:\Projects\MyJavaApp" --target-java 21

# Deep mode: allows longer Maven/Gradle operations and Maven version-plugin fallback
python auditor.py "C:\Projects\MyJavaApp" --target-java 21 --deep

# Air-gapped/offline mode
python auditor.py "C:\Projects\MyJavaApp" --offline
```

## Air-gapped Maven support

The auditor supports an internal Maven/Nexus/Artifactory repository. If the repository contains an `internal-maven` configuration file with a Maven `<url>`, that repository is used for dependency metadata instead of Maven Central.

The current repository contains:

```text
http://10.0.16.236:8081/repository/maven-public
```

In an air-gapped environment, use:

```powershell
python auditor.py "C:\Projects\MyJavaApp" --offline
```

The lookup order is:

1. Configured internal Maven repository
2. No public Maven Central lookup in offline mode
3. `CHECK_UNAVAILABLE` only when metadata cannot be obtained from the configured source

This keeps dependency/framework version analysis usable inside the internal network without requiring internet access.

If Maven Central or a build tool is unreachable, the auditor records the limitation instead of waiting indefinitely. Use `--deep` only when you specifically want the additional Maven version-plugin fallback.

## What it checks

### Project directories
- Java/build versions and module inventory
- Maven, Gradle and Ant build files
- Dependency versions and available updates
- Missing declared modules/files
- Selected deprecated API usage
- Framework compatibility recommendations
- Requested target-Java compatibility

### WAR/JAR/EAR/ZIP packages
- Compiled Java class versions
- Embedded library inventory
- Dependency/version conflicts and duplicate JARs
- Declared dependencies missing from the package
- Unresolved application class references
- Removed/deprecated-for-removal JDK APIs
- Internal JDK API usage such as `sun.*` and `Unsafe`
- `javax.*` to `jakarta.*` namespace usage
- Referenced configuration files that are missing
- Java baseline gap versus the requested target
- Migration readiness score and ordered migration plan

## Code structure

- `auditor.py` — command-line entry point
- `internal-maven` — internal Maven repository configuration
- `java_auditor/cli.py` — argument handling, repository routing, timeout modes and JSON output
- `java_auditor/config.py` — target Java and detection rules
- `java_auditor/build_files.py` — project build-file parsing
- `java_auditor/dependency_resolver.py` — Maven/Gradle dependency resolution
- `java_auditor/internal_maven.py` — internal Maven metadata lookup
- `java_auditor/version_checker.py` — version metadata and upgrade advice
- `java_auditor/project_scanner.py` — project completeness/deprecated API checks
- `java_auditor/report_builder.py` — project-directory report orchestration
- `java_auditor/artifact_report.py` — packaged-application report orchestration
- `java_auditor/artifact_build.py` — packaged build/Java/version detection
- `java_auditor/artifact_components.py` — packaged library inventory and conflicts
- `java_auditor/archive_source.py` — archive reader
- `java_auditor/classfile.py` — lightweight `.class` parser
- `java_auditor/bytecode_scanner.py` — bytecode compatibility and reference scanning
- `java_auditor/migration.py` — readiness score and ordered migration plan
- `java_auditor/rules/*.json` — data-driven compatibility and library rules

No third-party Python packages are required. Maven or Gradle is used only when available for project dependency/version information; packaged-application mode can operate without them.

## Important limitation

The auditor is an analysis tool. It does **not** start the application, run tests, automatically modify source code, or claim that a migration will compile successfully without a real build/test validation.
