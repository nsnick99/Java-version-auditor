import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auditor


class VersionAuditorTests(unittest.TestCase):
    def test_java_major_and_version_comparison(self):
        self.assertEqual(auditor.java_major("1.8"), "8")
        self.assertEqual(auditor.java_major("17"), "17")
        self.assertGreater(auditor.compare_versions("1.10.0", "1.9.9"), 0)
        self.assertLess(auditor.compare_versions("2.7.5", "3.0.0"), 0)
        self.assertLess(auditor.compare_versions("1.0.0-SNAPSHOT", "1.0.0"), 0)

    def test_finding_detects_conflicts(self):
        result = auditor.finding([
            ("11", "pom.xml > java.version"),
            ("17", "module/pom.xml > java.version"),
        ])
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["candidates"], ["11", "17"])

    def test_maven_resolves_property_and_local_dependency_management(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pom = root / "pom.xml"
            pom.write_text(
                """<project xmlns="http://maven.apache.org/POM/4.0.0">
                  <modelVersion>4.0.0</modelVersion>
                  <groupId>example</groupId>
                  <artifactId>sample</artifactId>
                  <version>1.0.0</version>
                  <parent>
                    <groupId>org.springframework.boot</groupId>
                    <artifactId>spring-boot-starter-parent</artifactId>
                    <version>3.2.1</version>
                    <relativePath/>
                  </parent>
                  <properties>
                    <java.version>17</java.version>
                    <sample.library.version>4.5.6</sample.library.version>
                  </properties>
                  <dependencyManagement>
                    <dependencies>
                      <dependency>
                        <groupId>org.example</groupId>
                        <artifactId>sample-library</artifactId>
                        <version>${sample.library.version}</version>
                      </dependency>
                    </dependencies>
                  </dependencyManagement>
                  <dependencies>
                    <dependency>
                      <groupId>org.example</groupId>
                      <artifactId>sample-library</artifactId>
                    </dependency>
                    <dependency>
                      <groupId>org.example</groupId>
                      <artifactId>other-library</artifactId>
                      <version>1.2.3</version>
                    </dependency>
                  </dependencies>
                </project>""",
                encoding="utf-8",
            )
            java, boot, components = auditor.maven(pom, root)
            self.assertIn(("17", "pom.xml > properties.java.version"), java)
            self.assertIn(("3.2.1", "pom.xml > parent.version"), boot)
            libraries = {item["artifact_id"]: item for item in components}
            self.assertEqual(libraries["sample-library"]["version"], "4.5.6")
            self.assertEqual(libraries["sample-library"]["version_source"], "dependency_management")
            self.assertEqual(libraries["other-library"]["version"], "1.2.3")

    def test_latest_maven_version_prefers_stable_release_metadata(self):
        metadata = b"""<metadata><groupId>org.example</groupId><artifactId>lib</artifactId>
        <versioning><latest>3.0.0-RC1</latest><release>2.7.5</release>
        <versions><version>2.7.5</version><version>3.0.0-RC1</version></versions>
        </versioning></metadata>"""
        with patch.object(auditor, "http_bytes", return_value=metadata):
            result = auditor.latest_maven_version("org.example", "lib")
        self.assertEqual(result["status"], "FOUND")
        self.assertEqual(result["version"], "2.7.5")

    def test_lifecycle_status_distinguishes_extended_support(self):
        product = {
            "status": "FOUND",
            "url": "https://endoflife.date/api/v1/products/spring-boot",
            "result": {
                "links": {"html": "https://endoflife.date/spring-boot"},
                "labels": {"eol": "OSS support", "eoes": "Commercial Support"},
                "releases": [{
                    "name": "2.7",
                    "isEol": True,
                    "isEoes": False,
                    "isMaintained": True,
                    "eolFrom": "2023-06-30",
                    "eoesFrom": "2029-06-30",
                    "latest": {"name": "2.7.18", "date": "2023-11-23"},
                }],
            },
        }
        result = auditor.lifecycle_status(product, "spring-boot", "2.7.5")
        self.assertEqual(result["status"], "OSS_SUPPORT_ENDED_EXTENDED_SUPPORT_AVAILABLE")
        self.assertEqual(result["latest_in_cycle"], "2.7.18")

    def test_osv_vulnerability_response_is_normalized(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.payload

        vulnerability = {
            "id": "GHSA-TEST-0000",
            "aliases": ["CVE-2025-0000"],
            "summary": "A test vulnerability",
            "database_specific": {"severity": "HIGH"},
            "affected": [{
                "package": {"ecosystem": "Maven", "name": "org.example:lib"},
                "ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.0.1"}]}],
            }],
            "references": [{"url": "https://example.com/advisory"}],
        }

        def fake_urlopen(request, timeout=8):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if url.endswith("/v1/querybatch"):
                return FakeResponse(b'{"results":[{"vulns":[{"id":"GHSA-TEST-0000"}]}]}')
            if url.endswith("/v1/vulns/GHSA-TEST-0000"):
                import json
                return FakeResponse(json.dumps(vulnerability).encode("utf-8"))
            raise AssertionError("Unexpected URL: " + url)

        item = {"group_id": "org.example", "artifact_id": "lib", "version": "1.0.0"}
        with patch.object(auditor.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = auditor.osv_query_batch([item])
        finding = result["findings"][("org.example", "lib", "1.0.0")]
        self.assertEqual(finding["status"], "VULNERABLE")
        self.assertEqual(finding["vulnerabilities"][0]["severity"], "HIGH")
        self.assertEqual(finding["vulnerabilities"][0]["fixed_versions"], ["1.0.1"])

    def test_compact_report_keeps_main_dependency_fields(self):
        raw = {
            "project": "sample",
            "project_path": "/tmp/sample",
            "build_system": "Maven",
            "detected_build_systems": ["Maven"],
            "module_count": 1,
            "modules": [{
                "name": "sample",
                "path": ".",
                "build_systems": ["Maven"],
                "build_files": ["pom.xml"],
                "java": {"status": "DETECTED", "version": "17", "evidence": [{"version": "17", "source": "pom.xml > properties.java.version"}]},
                "spring_boot": {"status": "NOT_DETECTED", "version": None, "evidence": []},
                "technology_updates": {
                    "java": {"recommended_version": "21", "lifecycle": {"status": "MAINTAINED"}},
                    "spring_boot": {"recommended_version": None, "lifecycle": {"status": "NOT_CHECKED"}},
                },
                "dependencies": [{
                    "group_id": "org.example", "artifact_id": "sample-lib", "version": "1.0.0",
                    "declared_version": "1.0.0", "version_source": "declared", "scope": "compile",
                    "optional": False, "direct": True, "component_type": "dependency", "source": "pom.xml",
                    "version_intelligence": {
                        "status": "FOUND", "latest_version": "1.2.0", "update_available": True,
                        "latest_source_url": "https://repo.maven.apache.org/maven2/org/example/sample-lib/maven-metadata.xml",
                        "lifecycle": {"status": "NOT_CHECKED"}, "deprecation": {"status": "NOT_VERIFIED"},
                        "vulnerability_status": "NO_MATCHING_ADVISORIES", "vulnerabilities": [],
                    },
                }],
            }],
            "dependency_version_conflicts": [],
            "version_inconsistencies": [],
            "dependency_resolution_requested": False,
            "version_recommendations": [],
            "version_intelligence": {
                "status": "COMPLETE", "checked_at": "2026-10-23T00:00:00+00:00",
                "vulnerability_scan": {"status": "COMPLETE"},
            },
        }
        result = auditor.compact_report(raw)
        self.assertEqual(result["summary"]["dependencies"], 1)
        self.assertEqual(result["summary"]["updates_available"], 1)
        self.assertEqual(result["dependencies"][0]["status"], "UPDATE_AVAILABLE")
        self.assertNotIn("project_path", result)
        self.assertNotIn("version_intelligence", result)

    def test_version_comparison_uncertainty_is_not_reported_as_current(self):
        raw = {
            "project": "sample",
            "module_count": 1,
            "modules": [{
                "path": ".",
                "java": {"status": "NOT_DETECTED", "version": None, "evidence": []},
                "spring_boot": {"status": "NOT_DETECTED", "version": None, "evidence": []},
                "technology_updates": {
                    "java": {"recommended_version": None, "lifecycle": {"status": "NOT_CHECKED"}},
                    "spring_boot": {"recommended_version": None, "lifecycle": {"status": "NOT_CHECKED"}},
                },
                "dependencies": [{
                    "group_id": "org.example", "artifact_id": "custom-lib", "version": "2024-release",
                    "component_type": "dependency", "source": "pom.xml",
                    "version_intelligence": {
                        "status": "COMPARISON_UNSUPPORTED", "latest_version": "2025-release",
                        "update_available": None, "lifecycle": {"status": "NOT_CHECKED"},
                        "deprecation": {"status": "NOT_VERIFIED"}, "vulnerability_status": "NOT_CHECKED",
                        "vulnerabilities": [],
                    },
                }],
                "dependency_resolution": {"status": "NOT_REQUESTED"},
            }],
            "dependency_version_conflicts": [],
            "version_inconsistencies": [],
            "dependency_resolution_requested": False,
            "version_recommendations": [],
            "version_intelligence": {
                "status": "COMPLETE", "checked_at": "2026-10-23T00:00:00+00:00",
                "vulnerability_scan": {"status": "COMPLETE"},
            },
        }
        result = auditor.compact_report(raw)
        self.assertEqual(result["dependencies"][0]["status"], "VERSION_COMPARISON_UNCERTAIN")
        self.assertEqual(result["checks"]["vulnerabilities"], "AVAILABLE_WITH_GAPS")

    def test_missing_build_files_are_reported_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = auditor.audit(Path(tmp), online=False)
            self.assertFalse(report["scan"]["project_detected"])
            self.assertIn("No Maven", report["scan"]["warning"])
            self.assertEqual(report["summary"]["modules"], 0)

    def test_known_maintenance_advisory(self):
        result = auditor.known_deprecation({
            "group_id": "com.netflix.hystrix",
            "artifact_id": "hystrix-core",
        })
        self.assertEqual(result["status"], "MAINTENANCE_MODE")
        self.assertIn("Resilience4j", result["recommended_alternative"])

    def test_offline_audit_does_not_require_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pom.xml").write_text(
                """<project xmlns="http://maven.apache.org/POM/4.0.0">
                  <modelVersion>4.0.0</modelVersion>
                  <groupId>example</groupId><artifactId>sample</artifactId><version>1.0.0</version>
                  <properties><java.version>17</java.version></properties>
                </project>""",
                encoding="utf-8",
            )
            report = auditor.audit(root, online=False)
            self.assertEqual(report["summary"]["modules"], 1)
            java_tech = next(item for item in report["technologies"] if item["name"] == "Java")
            self.assertEqual(java_tech["current_version"], "17")
            self.assertEqual(report["scan"]["mode"], "OFFLINE")
            self.assertEqual(report["checks"]["latest_versions"], "SKIPPED")
            self.assertEqual(
                set(report.keys()),
                {"schema_version", "project", "scan", "summary", "technologies", "dependencies", "alerts", "version_conflicts", "checks"},
            )


if __name__ == "__main__":
    unittest.main()
