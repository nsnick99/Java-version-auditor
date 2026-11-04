# Java Version Auditor

One command audits a Java project directory **or a packaged `.war/.jar/.ear/.zip`** and writes `output.json`. The report focuses on Java-version compatibility, dependency intelligence, API usage, security signals and packaging findings.

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

The default mode is **fast**. It uses bounded network/tool timeouts and skips the Maven Versions Plugin fallback, which is often the slowest part of a large Maven scan.

```powershell
# Fast/default mode
python auditor.py "C:\Projects\MyJavaApp"

# Explicit target Java
python auditor.py "C:\Projects\MyJavaApp" --target-java 21

# Deep mode: allows longer Maven/Gradle operations and Maven version-plugin fallback
python auditor.py "C:\Projects\MyJavaApp" --target-java 21 --deep

# Strict offline mode: no repository/network access and no Maven/Gradle network resolution
python auditor.py "C:\Projects\MyJavaApp" --offline
```

### Resolution modes

The auditor automatically selects the resolution source:

1. **Internal** — if the `internal-maven` configuration contains a repository URL, dependency/framework metadata is read from that repository. Maven/Gradle dependency-tree execution is not used, so an air-gapped scan does not hang on plugin or transitive-resolution timeouts.
2. **Offline** — `--offline` disables repository access and uses only information already present in the build files/local analysis. Missing information is reported as unavailable rather than being guessed.
3. **Normal** — when no internal repository is configured, Maven/Gradle resolution and Maven metadata lookup are used according to the selected fast/deep mode.

The report contains a `resolution` object showing which mode was actually used.

## Air-gapped Maven support

The auditor supports an internal Maven/Nexus/Artifactory repository. If the repository contains an `internal-maven` configuration file with a Maven `<url>`, that repository is used for dependency metadata instead of Maven Central.

The current repository contains:

```text
http://10.0.16.236:8081/repository/maven-public
```

For an internal-network scan, run normally:

```powershell
python auditor.py "C:\Projects\MyJavaApp"
```

Do **not** add `--offline` when you want the internal repository queried; `--offline` intentionally disables all repository access.

Internal Maven results distinguish `FOUND`, `NOT_FOUND`, `AUTH_REQUIRED`, `TIMEOUT`, `INVALID_METADATA`, `UNAVAILABLE`, and `NO_STABLE_RELEASE` instead of collapsing all failures into one unresolved status.

## What it checks

### Project directories
- Java/build versions and module inventory
- Maven, Gradle and Ant build files
- Dependency versions and available updates
- Inherited Spring Boot dependency versions
- Missing declared modules/files
- Selected deprecated API usage
- Framework compatibility recommendations
- Conservative hardcoded-secret detection in project configuration files
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

## Code structure

- `auditor.py` — command-line entry point
- `internal-maven` — internal Maven repository configuration
- `java_auditor/cli.py` — argument handling, repository routing, timeout modes and JSON output
- `java_auditor/config.py` — target Java, timeout and detection rules
- `java_auditor/build_files.py` — project build-file parsing and inherited version handling
- `java_auditor/dependency_resolver.py` — Maven/Gradle dependency resolution and internal/offline routing
- `java_auditor/internal_maven.py` — internal Maven metadata lookup and diagnostics
- `java_auditor/version_checker.py` — version metadata and stable upgrade advice
- `java_auditor/project_scanner.py` — project completeness/deprecated API checks
- `java_auditor/security_scanner.py` — redacted hardcoded-secret detection
- `java_auditor/report_builder.py` — project-directory report orchestration
- `java_auditor/artifact_report.py` — packaged-application report orchestration
- `java_auditor/artifact_build.py` — packaged build/Java/version detection
- `java_auditor/artifact_components.py` — packaged library inventory and conflicts
- `java_auditor/archive_source.py` — archive reader
- `java_auditor/classfile.py` — lightweight `.class` parser
- `java_auditor/bytecode_scanner.py` — bytecode compatibility and reference scanning
- `java_auditor/rules/*.json` — data-driven compatibility and library rules

No third-party Python packages are required. Maven or Gradle is used only when available and appropriate for normal-mode project dependency/version information; packaged-application mode can operate without them.

## Important limitation

The auditor is an analysis tool. It does **not** start the application, run tests, automatically modify source code, or claim that a migration will compile successfully without a real build/test validation.
