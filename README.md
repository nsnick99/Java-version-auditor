# Java Version Auditor

A focused Java project auditor that lists **only components needing attention**. Components with no detected issue are summarized as OK instead of being repeated in the report. It does not calculate an overall migration-readiness score.

## Run

Online mode checks dependency versions against Maven Central and lifecycle data through the configured public lifecycle source:

~~~bash
python main.py /path/to/java-project --output audit-report.json
~~~

**Internal-only mode** queries Maven metadata only from private/local Maven repositories declared in the project's actual pom.xml. It never falls back to Maven Central and skips public lifecycle APIs:

~~~bash
python main.py /path/to/java-project --internal-only --resolve --output audit-report.json
~~~

The resolve option asks Maven to generate the effective POM so parent/BOM-managed versions can be inspected. In internal-only mode, a temporary Maven settings file mirrors all Maven repositories to the approved internal endpoint. If resolution fails, check Maven installation, repository credentials, parent POMs, and whether the required Maven Help Plugin is available internally.

**Strict zero-network mode** performs static inspection only:

~~~bash
python main.py /path/to/java-project --offline --output audit-report.json
~~~

Do not combine internal-only and offline. Internal-only mode makes requests to approved private/local Maven endpoints; offline mode makes no HTTP requests.

Only Maven projects are currently audited. Python 3.10+ is required. Maven is required only for resolve.

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

- Maven Central metadata for online latest-version checks
- Private/local Maven repository metadata for internal-only version checks
- endoflife.date for Java and Spring Boot lifecycle checks in online mode
- Locally encoded maintenance-mode advisories (a small rule list, not a complete advisory database)

## Limitations

- Internal-only mode accepts private IPs, loopback addresses, and hostnames ending in .local or .internal. It also permits unqualified intranet hostnames when the repository ID clearly indicates an internal repository.
- Repository URLs are read from the scanned project's pom.xml. The auditor does not parse edit.txt.
- Internal Maven metadata can compare versions only for artifacts published or proxied by that repository. A missing metadata file may mean the repository does not host or proxy that artifact.
- Vulnerability checks are not currently implemented. Lifecycle checks are skipped in internal-only and offline modes because Maven metadata alone cannot establish support status.
- Without resolve, versions inherited from parent POMs or imported BOMs may remain unresolved. The report includes VERSION_UNRESOLVED instead of treating them as current.
- In internal-only mode, resolve mirrors Maven repositories to the internal endpoint using temporary settings. If required artifacts or plugins are unavailable internally, resolution fails rather than falling back to public Maven Central.
- A newer version is not necessarily compatible; check release notes and tests before upgrading.
- Offline skips all network-based version and lifecycle checks. When resolve is also used, Maven is told to use its local cache only.
- output.json is a generated snapshot and needs to be regenerated after code changes.

## Out of scope

The analyzer does not generate migration-readiness scores, migration paths, or automatic code changes.
