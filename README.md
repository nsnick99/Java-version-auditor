# Java Version Auditor

A focused Java project version auditor. It reports the technologies and dependency versions found, whether newer versions are available, known vulnerability alerts, lifecycle warnings, and version conflicts. It does **not** calculate migration readiness.

## Run

Online checks are enabled by default:

~~~bash
python main.py /path/to/java-project
~~~

Save the concise JSON report:

~~~bash
python main.py /path/to/java-project --output audit-report.json
~~~

Skip external checks:

~~~bash
python main.py /path/to/java-project --offline
~~~

Ask Maven/Gradle to resolve transitive dependencies (optional):

~~~bash
python main.py /path/to/java-project --resolve
~~~

Use Python 3.8+. No third-party Python packages are required. Maven or Gradle is only needed for --resolve.

## Output

The JSON uses a stable, concise schema:

- summary: module/dependency counts, updates, vulnerability alerts, lifecycle warnings, and version conflicts
- technologies: Java and Spring Boot current/recommended version per module
- dependencies: component coordinates, current/latest version, status, vulnerability summaries, lifecycle status, and source file
- alerts: vulnerability, lifecycle, and maintenance advisories; update versions are already shown on each dependency
- version_conflicts: versions of the same component that differ across modules
- checks: whether external checks were completed, skipped, or partially available

Statuses such as VERSION_CHECK_UNAVAILABLE, VERSION_UNKNOWN, NO_ADVISORY_REPORTED, and PARTIAL distinguish missing data from a clean result. The report omits large API payloads, repeated source metadata, and unnecessary request details.

## Data sources

- Maven Central metadata for latest stable published versions
- OSV.dev for known vulnerability advisories
- endoflife.date for lifecycle data where a product/release cycle is catalogued
- Eclipse Adoptium for a latest-LTS Java reference

## Resolved dependencies and safety

Static parsing may not discover transitive versions or remote BOM-managed versions. Use --resolve only for repositories you trust: it invokes Maven or Gradle and executes the project build configuration. It may download plugins and dependencies.

## Limitations

- A newer version is not automatically compatible with the project. Major-version changes must be reviewed.
- Maven Central checks do not cover private repositories or packages not published there.
- Without --resolve, some transitive dependencies may be missing or unresolved.
- Gradle static parsing supports common dependency notations and version catalogs, not every custom build convention.
- No OSV match does not prove a component is vulnerability-free.
- Lifecycle data is only available for products present in the selected dataset; Java support also depends on the JDK vendor.
- Deprecation/maintenance status is only shown when there is a source-backed advisory. NOT_VERIFIED does not mean actively maintained.

## Out of scope

Migration readiness scores, migration blockers, migration planning, and rewrite estimates are intentionally excluded.
