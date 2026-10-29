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
