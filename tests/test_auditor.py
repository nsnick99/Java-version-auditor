import sys
import tempfile
import unittest
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
            self.assertEqual(report["module_count"], 1)
            self.assertEqual(report["modules"][0]["java"]["version"], "17")
            self.assertEqual(report["version_intelligence"]["status"], "SKIPPED")


if __name__ == "__main__":
    unittest.main()
