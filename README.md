# Java Version Auditor

One command scans a Java project and writes `output.json`. The report intentionally lists only components that need an update or a specific compatibility change.

## Run

```bash
python auditor.py /path/to/java-project
```

On Windows:

```powershell
python auditor.py "C:\Users\YourName\Desktop\Java-test"
```

The script writes `output.json` in the scanned Java project's root directory. The report includes `auditor_version`, `generated_at`, and `project_path` so you can verify which run produced it. There are no optional command-line modes.

## Code structure

The implementation is split into focused Python modules:

- `auditor.py`: small entry point; keeps the same one-command usage
- `java_auditor/cli.py`: command-line execution and JSON output
- `java_auditor/config.py`: detection rules and constants
- `java_auditor/utils.py`: shared text, path, version, and process helpers
- `java_auditor/build_files.py`: Maven, Gradle, and Ant build-file parsing
- `java_auditor/dependency_resolver.py`: Maven/Gradle dependency-tree resolution
- `java_auditor/version_checker.py`: Maven Central metadata and framework/dependency version advice
- `java_auditor/project_scanner.py`: missing Gradle-module checks and selected deprecated API scans
- `java_auditor/report_builder.py`: audit orchestration and report construction

Run it from the repository folder using the same command shown above. No third-party Python packages are required.

## What appears in the report

- Java/build versions and module inventory
- `frameworks`: only frameworks with a confirmed newer version or a known Java compatibility issue
- `dependencies`: only dependencies with an update available
- `latest_version`, `recommended_version`, and a non-empty `version_status` for actionable updates
- `missing_files`: missing declared modules, explicit parent paths, system-path dependencies, resource folders, and Gradle subprojects
- `issues` and `recommendations`: concise details for action

Java versions below JDK 8 are flagged with `NEEDS_CHANGES` and a recommendation to move to at least Java 8. The analyzer does not flag Java 11 solely because a former default target was Java 21.

## Version resolution

For Maven projects, the scanner uses `mvn dependency:tree` to read the resolved dependency versions. It also invokes Maven Versions Plugin reporting goals to find newer dependency and parent versions, including versions inherited through dependency management or a Spring Boot BOM. A report goal checks versions only; the analyzer does not start the app or run tests.

Maven Central metadata is queried as an additional source. A dependency is listed as needing an update only when the scanner can establish a newer version. Frameworks that are already current are omitted. The old redundant `versions` array is not included in framework entries.

If a tool/repository check cannot establish newer versions, the script does not invent a recommended version. A missing Maven/Gradle dependency version is reported as unresolved when applicable.

## Excluded

- Application runtime / health checks
- Build and test validation
- Vulnerability and EOL scans
- Migration readiness scores or migration plans
- Automatic source-code changes

No third-party Python packages are required. Maven or Gradle is used only to read dependency trees/version metadata.
