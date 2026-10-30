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

## What it checks

### Project directories
- Java/build versions and module inventory
- Maven, Gradle and Ant build files
- Dependency versions and available updates
- Missing declared modules/files
- Selected deprecated API usage
- Framework compatibility recommendations

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
- `java_auditor/cli.py` — argument handling and JSON output
- `java_auditor/config.py` — target Java and detection rules
- `java_auditor/build_files.py` — project build-file parsing
- `java_auditor/dependency_resolver.py` — Maven/Gradle dependency resolution
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
