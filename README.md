# Java Version Auditor

A standalone analyzer for a Java project’s build systems, Java/Spring Boot versions, library dependencies, available updates, lifecycle status, and known vulnerability advisories. This is a version-auditing tool, **not** a migration-readiness analyzer.

## Requirements

- Python 3.8+
- Standard library only; no Python packages to install
- Internet access for online version/security checks
- Maven or Gradle installed only if you use the optional \`--resolve\` mode

## Run

Online checks are enabled by default:

~~~bash
python main.py /path/to/java-project
~~~

Choose a report path:

~~~bash
python main.py /path/to/java-project --output audit-report.json
~~~

Run static inventory without internet calls:

~~~bash
python main.py /path/to/java-project --offline
~~~

Resolve transitive dependency versions (optional):

~~~bash
python main.py /path/to/java-project --resolve
~~~

The equivalent direct CLI is available:

~~~bash
python auditor.py /path/to/java-project --output audit-report.json
~~~

Both commands accept \`--offline\` and \`--resolve\`.

## What it checks

- Maven, Gradle, and Ant build files and project modules
- Java and Spring Boot versions, with source evidence
- Direct Maven dependencies and versions managed by locally available parent POMs or BOMs
- Basic Gradle dependency declarations and Gradle version-catalog aliases
- Explicit Maven/Gradle build plugin versions
- Version differences and conflicts
- Newer stable artifact releases published to Maven Central
- Known vulnerabilities returned by OSV.dev for resolved Maven coordinates and versions
- End-of-life/maintenance status from endoflife.date when a matching product and release cycle are catalogued
- Source-backed maintenance-mode advisories for known legacy libraries, with alternatives to investigate
- A JSON list of recommendations in the form **current version → newer version**

## Data sources

- Maven Central repository metadata: latest published stable versions for Maven coordinates
- OSV.dev: known vulnerabilities by package coordinates and version
- endoflife.date API: lifecycle information for catalogued products
- Eclipse Adoptium API: current LTS Java release reference

The JSON report includes the source URL for version lookups/advisories wherever available, a scan timestamp, and statuses such as \`UNAVAILABLE\`, \`NOT_CATALOGUED\`, and \`VERSION_NOT_RESOLVED\`. These statuses are intentionally different from a clean result.

## Optional resolved dependency graph

Static parsing can miss transitive versions and versions supplied by remote parent POMs/BOMs. Use \`--resolve\` when you trust the project and its build scripts. The analyzer will invoke Maven’s \`dependency:tree\` or Gradle’s \`dependencies\` task and include versions reported by the build tool. This may download build plugins/dependencies and **executes project build configuration**; do not enable it for an untrusted project without reviewing it first.

## Important limitations

- A newer version is not automatically a safe or compatible upgrade. The report flags major-version changes for review but does not claim compatibility.
- Maven Central latest-version checks do not cover private repositories or artifacts that are not published to Maven Central.
- Without \`--resolve\`, transitive dependencies and versions managed only by remote BOMs may be absent or unresolved.
- Gradle parsing is static and supports common notations; complex convention plugins, custom catalogs, dynamic versions, and build logic can require \`--resolve\`.
- Vulnerability results mean that OSV returned a matching advisory at scan time; no returned advisory does not prove a dependency is vulnerability-free.
- Lifecycle information is only reported for products available in the lifecycle dataset. Java support depends on the JDK vendor/distribution.
- Dependency deprecation is only reported where an explicit, source-backed advisory rule exists. \`NOT_VERIFIED\` means the analyzer could not verify a deprecation status, not that the library is actively maintained.
- Ant JAR-name inventory is heuristic and does not reliably recover every coordinate.
- Online mode sends package coordinates and versions to Maven Central/OSV and requests lifecycle metadata from endoflife.date. Offline mode skips those requests.

## Scope exclusions

The tool does not calculate migration readiness, migration blockers, migration paths, migration effort, or rewrite-from-scratch classifications.
