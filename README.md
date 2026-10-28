# Java Version Auditor

A focused Java project auditor that lists **only components needing attention**. Components with no detected issue are summarized as OK instead of being repeated in the report. It does not calculate an overall migration-readiness score.

## Run

~~~bash
python main.py /path/to/java-project
~~~

Save the report to a JSON file:

~~~bash
python main.py /path/to/java-project --output audit-report.json
~~~

Skip online checks:

~~~bash
python main.py /path/to/java-project --offline
~~~

Resolve Maven/Gradle transitive dependencies (optional):

~~~bash
python main.py /path/to/java-project --resolve
~~~

Use Python 3.8+. No third-party Python packages are required. Maven or Gradle is required only for --resolve.

## Report format

- summary: overall result, counts of components scanned/OK/needing review, and issue counts
- review_items: only components with updates, known vulnerabilities, lifecycle warnings, version conflicts, unresolved versions, or incomplete checks
- checks: whether version, vulnerability, and lifecycle checks completed

The summary says REVIEW_REQUIRED when findings exist, and its message indicates if any checks are incomplete. It says CHECK_INCOMPLETE when no findings were raised but required checks were skipped or incomplete. Components are counted as OK only when the required version, security, and applicable lifecycle checks support that conclusion.

Each review item gives the module, component, current version, issue types, concise reasons, and a recommended action. When a lookup fails, an issue includes available diagnostic details such as an HTTP status or network error. Healthy components are not listed individually.

The root-level output.json is a generated report snapshot, not a live view. Regenerate it against the target Java project after changing the auditor:

~~~bash
python main.py /path/to/Java_Full_Stack_Project --output output.json
~~~

## Analysis sources

- Maven Central metadata for current stable releases
- OSV.dev for known vulnerability advisories
- endoflife.date for lifecycle data where a product/release cycle is catalogued
- Eclipse Adoptium for the latest LTS Java reference

## Limitations

- A newer release is not automatically compatible; review release notes and tests before updating.
- Maven Central checks do not cover private repositories.
- Without --resolve, some transitive dependencies and remotely managed versions may not be discovered.
- With --resolve, Maven's effective POM is also inspected to identify parent/BOM-managed dependency and plugin versions and inherited Java compiler settings when the required Maven plugins and metadata are available locally.
- Combine --offline and --resolve in air-gapped environments. The auditor passes Maven's offline flag or Gradle's --offline option to prevent the build tool from trying remote repositories. Effective-POM and dependency resolution can still fail when required plugins, parent POMs, or artifacts are not cached locally.
- Gradle static parsing supports common dependency notations and version catalogs, not every custom convention.
- No OSV match does not prove a dependency is vulnerability-free.
- Lifecycle coverage depends on the available lifecycle dataset.
- Maintenance/deprecation advisories are source-backed rules for known components, not a universal deprecation database.
- --resolve executes Maven/Gradle project build configuration; use it only on trusted projects.

## Out of scope

The analyzer does not generate migration-readiness scores, migration paths, or automatic code changes.
