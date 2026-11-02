"""Central configuration and detection rules."""

import os
import re

AUDITOR_VERSION = "4.1"
TARGET_JAVA = 21
ARTIFACT_EXTENSIONS = {".war", ".jar", ".ear", ".zip"}
MINIMUM_JAVA_VERSION = "8"
HTTP_TIMEOUT = int(os.getenv("JAVA_AUDITOR_HTTP_TIMEOUT", "5"))
TOOL_TIMEOUT = int(os.getenv("JAVA_AUDITOR_TOOL_TIMEOUT", "30"))
SKIP_DIRS = {".git", ".idea", "target", "build", "out", "bin", "node_modules", ".gradle", "__pycache__", ".venv", "venv"}
BUILD_NAMES = {"pom.xml", "build.gradle", "build.gradle.kts", "build.xml"}
SETTINGS_NAMES = {"settings.gradle", "settings.gradle.kts"}
USER_AGENT = "Java-Version-Auditor/4.1"
PLACEHOLDER_START = "$" + "{"
# Maven metadata uses several abbreviated pre-release forms (M1, RC1, etc.).
PRE_RELEASE = re.compile(r"(?i)(?:^|[.\-_])(alpha|beta|milestone|preview|nightly|dev|ea|m|rc)(?:[.\-_]?\d+)?(?:$|[.\-_])|(?:snapshot)")

FRAMEWORK_RULES = [
    ("Spring Boot", [("org.springframework.boot", "spring-boot"), ("org.springframework.boot", "spring-boot-autoconfigure")]),
    ("Spring MVC", [("org.springframework", "spring-webmvc")]),
    ("Spring Framework", [("org.springframework", "spring-core")]),
    ("Spring Security", [("org.springframework.security", "spring-security-core"), ("org.springframework.security", "spring-security-web"), ("org.springframework.security", "spring-security-crypto")]),
    ("Hibernate", [("org.hibernate", "hibernate-core"), ("org.hibernate.orm", "hibernate-core")]),
    ("Struts", [("org.apache.struts", "struts2-core"), ("org.apache.struts", "struts-core")]),
    ("JSF", [("org.glassfish", "jakarta.faces"), ("org.glassfish", "javax.faces"), ("com.sun.faces", "jsf-impl"), ("javax.faces", "jsf-api"), ("jakarta.faces", "jakarta.faces-api")]),
    ("Apache Camel", [("org.apache.camel", "camel-core-engine"), ("org.apache.camel", "camel-core")]),
    ("JPA", [("jakarta.persistence", "jakarta.persistence-api"), ("javax.persistence", "javax.persistence-api"), ("org.hibernate.javax.persistence", "hibernate-jpa-2.1-api")]),
]

DEPRECATED_API_RULES = [
    (re.compile(r"\bsun\.misc\."), "sun.misc API", "Internal JDK API; direct use can be fragile on newer JDKs."),
    (re.compile(r"\bjavax\.xml\.bind\."), "JAXB API", "JAXB is no longer bundled with the JDK from Java 11 onward; add a supported dependency if needed."),
    (re.compile(r"\bSecurityManager\b"), "SecurityManager", "SecurityManager is deprecated for removal in modern Java releases."),
    (re.compile(r"\bjava\.util\.Observable\b|\bjava\.util\.Observer\b"), "Observable/Observer", "This API is deprecated; consider a modern event mechanism."),
]
