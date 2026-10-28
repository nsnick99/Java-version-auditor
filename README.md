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

Skip external checks but allow a configured internal repository to supply Maven/Gradle dependencies:

~~~bash
python main.py /path/to/java-project --offline --resolve --output audit-report.json
~~~

Force the build tools to use only their local caches:

~~~bash
python main.py /path/to/java-project --offline --build-offline --resolve --output audit-report.json
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

- Maven Central metadata for current stable releases in online mode
- Project-configured Maven repository metadata for version comparisons in `--offline` mode, when HTTP(S) repository URLs are declared in a POM
- OSV.dev for known vulnerability advisories (online mode only)
- endoflife.date for lifecycle data where a product/release cycle is catalogued (online mode only)
- Eclipse Adoptium for the latest LTS Java reference (online mode only)

## Limitations

- A newer release is not automatically compatible; review release notes and tests before updating.
- Online latest-version checks use Maven Central. In `--offline` mode, latest-version metadata is queried only from HTTP(S) repositories declared in project POMs; repositories from `settings.xml` are not currently discovered by the auditor.
- Offline mode can check dependency/Spring Boot versions against internal Maven metadata, but vulnerability and lifecycle checks remain SKIPPED unless separate internal advisory/lifecycle data sources are added.
- Without --resolve, some transitive dependencies and remotely managed versions may not be discovered.
- With --resolve, Maven's effective POM is also inspected to identify parent/BOM-managed dependency and plugin versions and inherited Java compiler settings when the required Maven plugins and metadata are available locally.
- Use --offline --resolve when public advisory APIs are blocked but a Maven repository declared in the project POM is reachable. The auditor queries that repository's Maven metadata for latest versions and still skips public vulnerability/lifecycle APIs. Maven/Gradle can also use configured repositories to resolve dependencies. The internal repository URL is not included in the JSON report.
- Use --build-offline --resolve only when no repository is reachable and all required dependencies, plugins, and parent POMs are already cached locally. This passes Maven's -o flag or Gradle's --offline option.
- Effective-POM and dependency resolution can still fail when required plugins, parent POMs, or artifacts are unavailable from the reachable internal repository or local cache.
- edit.txt is an XML fragment for adding repository/pluginRepository entries to a POM; the auditor does not parse edit.txt itself.
- Gradle static parsing supports common dependency notations and version catalogs, not every custom convention.
- No OSV match does not prove a dependency is vulnerability-free.
- Lifecycle coverage depends on the available lifecycle dataset.
- Maintenance/deprecation advisories are source-backed rules for known components, not a universal deprecation database.
- --resolve executes Maven/Gradle project build configuration; use it only on trusted projects.

## Out of scope

The analyzer does not generate migration-readiness scores, migration paths, or automatic code changes.
