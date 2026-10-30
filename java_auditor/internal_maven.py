"""Internal Maven repository metadata lookup for air-gapped environments."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .config import HTTP_TIMEOUT, PRE_RELEASE, USER_AGENT
from .utils import child, node_text, version_sort_key


def maven_metadata(group, artifact, base_url):
    path = "/".join(urllib.parse.quote(piece, safe="") for piece in group.split("."))
    artifact_path = urllib.parse.quote(artifact, safe="")
    url = base_url.rstrip("/") + f"/{path}/{artifact_path}/maven-metadata.xml"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            root = ET.fromstring(response.read())
        versioning = child(root, "versioning")
        version_node = child(versioning, "versions")
        versions = [(x.text or "").strip() for x in list(version_node or []) if x.text]
        stable = sorted({v for v in versions if not PRE_RELEASE.search(v)}, key=version_sort_key)
        if not stable:
            release = node_text(versioning, "release")
            stable = [release] if release and not PRE_RELEASE.search(release) else []
        return {"status": "FOUND", "versions": stable, "latest_version": stable[-1], "source": url} if stable else {"status": "NO_STABLE_RELEASE", "versions": [], "source": url}
    except urllib.error.HTTPError as exc:
        return {"status": "NOT_FOUND" if exc.code == 404 else "UNAVAILABLE", "versions": [], "source": url}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "versions": [], "source": url, "message": str(exc)[:140]}
