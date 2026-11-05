"""Prepare metadata-only Codecov inputs and verify their processed delivery.

Executed only from the immutable default-branch workflow revision, never from
producer code. Candidate trees and reports are data; no source blobs are fetched.
"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

PACKAGE = "custom_components/ha_energy_planner"
MAX_RESPONSE = 16 * 1024 * 1024


def relative_path(value: str) -> str:
    """Accept canonical repository paths, never filesystem or protocol escapes."""
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"..", ".git"} for part in path.parts)
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Noncanonical source path")
    return value


def read_xml(path: Path) -> ET.Element:
    data = path.read_bytes()
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML declarations are not supported")
    return ET.fromstring(data)


def prepare(tree: dict, sha: str, reports: Path, inventory: Path, manifest: Path) -> None:
    """Create inert file placeholders and map coverage by exact producer paths."""
    if tree.get("commit") != sha or tree.get("truncated") is not False:
        raise ValueError("Incomplete or mismatched producer tree")
    entries = tree["tree"]
    if not 0 < len(entries) <= 50000:
        raise ValueError("Source inventory budget exceeded")
    files = set()
    for entry in entries:
        name = relative_path(entry["path"])
        if entry["type"] == "blob" and entry["mode"] in {"100644", "100755"}:
            if name in files:
                raise ValueError("Duplicate source path")
            files.add(name)
    root = read_xml(reports / "coverage.xml")
    if root.tag != "coverage":
        raise ValueError("Expected Cobertura coverage")
    sources = root.find("sources")
    if sources is None:
        raise ValueError("Missing coverage sources")
    prefixes = set()
    for source in sources.findall("source"):
        prefix = (source.text or "").removeprefix("/work/").rstrip("/")
        if prefix in {"", ".", "/work"}:
            prefix = ""
        elif prefix != PACKAGE:
            raise ValueError("Unexpected producer coverage source")
        prefixes.add(prefix)
    if not prefixes:
        raise ValueError("Missing coverage source paths")
    mapped = set()
    line_count = 0
    for cls in root.findall("./packages/package/classes/class"):
        filename = relative_path(cls.attrib["filename"].removeprefix("/work/"))
        candidates = {f"{prefix}/{filename}" if prefix else filename for prefix in prefixes}
        if filename.startswith(PACKAGE + "/"):
            candidates.add(filename)
        matches = {path for path in candidates if path in files and path.startswith(PACKAGE + "/")}
        if len(matches) != 1:
            raise ValueError("Coverage path has no unique regular source file: " + filename)
        name = matches.pop()
        if name in mapped:
            raise ValueError("Duplicate coverage file")
        mapped.add(name)
        cls.set("filename", name)
        for line in cls.findall("./lines/line"):
            if int(line.attrib["number"]) <= 0 or int(line.attrib["hits"]) < 0:
                raise ValueError("Invalid coverage line")
            line_count += 1
    if not mapped or not line_count:
        raise ValueError("Empty coverage report")
    junit = read_xml(reports / "junit.xml")
    if junit.tag not in {"testsuites", "testsuite"} or not list(junit.iter("testcase")):
        raise ValueError("Empty test results")
    inventory.mkdir()  # A fresh directory outside the workspace; no .git or source code.
    for name in sorted(files):
        path = inventory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=False)
    sources.clear()
    ET.SubElement(sources, "source").text = "."
    normalized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    other_size = sum(
        path.stat().st_size for path in (reports / "junit.xml", reports / "coverage.json") if path.exists()
    )
    if len(normalized) > MAX_RESPONSE or len(normalized) + other_size > 2 * MAX_RESPONSE:
        raise ValueError("Normalized report budget exceeded")
    (reports / "coverage.xml").write_bytes(normalized)
    manifest.write_text(json.dumps({"commit": sha, "files": sorted(mapped), "lines": line_count}))
    print(f"Prepared {len(files)} inert source paths and {len(mapped)} covered files ({line_count} lines)")


def fetch_json(url: str) -> dict:
    """Anonymous public API access; never attach GitHub or OIDC credentials."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.codecov.io":
        raise ValueError("Unexpected Codecov API URL")
    with urlopen(url, timeout=10) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError("Codecov response budget exceeded")
    return json.loads(data)


def uploads_for(url: str, fetch=fetch_json) -> list[dict]:
    """Read bounded pagination, including attempts beyond the first page."""
    uploads = []
    seen = set()
    for _ in range(20):
        if url in seen:
            raise ValueError("Cyclic upload pagination")
        seen.add(url)
        page = fetch(url)
        uploads.extend(page["results"])
        following = page.get("next")
        if not following:
            return uploads
        next_url = urljoin(url, following)
        if urlsplit(next_url).path != urlsplit(url).path:
            raise ValueError("Unexpected upload pagination path")
        url = next_url
    raise ValueError("Upload pagination budget exceeded")


def delivery_ready(commit: dict, uploads: list[dict], expected: dict, run: str, attempt: str) -> bool:
    """Require this attempt's processed uploads and nonempty project coverage."""
    selected = []
    for kind in ("coverage", "junit"):
        name = f"{kind}-{run}-{attempt}"
        matches = [
            upload
            for upload in uploads
            if upload.get("name") == name and upload.get("build_code") == f"{run}.{attempt}"
        ]
        if not matches:
            return False
        if any(upload.get("state") == "error" for upload in matches):
            raise ValueError(f"Codecov rejected {name}")
        if len(matches) != 1:
            raise ValueError(f"Ambiguous Codecov upload {name}")
        selected.append(matches[0])
    coverage, junit = selected
    if any(upload.get("state") != "processed" for upload in selected):
        return False
    if junit.get("job_code") != f"junit-{run}-{attempt}" or not junit.get("storage_path", "").startswith(
        "test_results/v1/raw/"
    ):
        raise ValueError("Unexpected processed test results attribution")
    totals = coverage.get("totals") or {}
    if not totals.get("files", 0) or not totals.get("lines", 0):
        raise ValueError("Processed coverage upload has no coverage")
    if commit.get("commitid") != expected["commit"]:
        raise ValueError("Wrong processed commit")
    if commit.get("state") == "error":
        raise ValueError("Codecov commit processing failed")
    if commit.get("state") != "complete":
        return False
    totals = commit.get("totals") or {}
    files = (commit.get("report") or {}).get("files") or []
    actual = {file["name"] for file in files if (file.get("totals") or {}).get("lines", 0) > 0}
    if not totals.get("files", 0) or not totals.get("lines", 0) or not set(expected["files"]) <= actual:
        raise ValueError("Processed project coverage is empty or missing source files")
    return True


def verify(
    expected: dict, slug: str, run: str, attempt: str, *, fetch=fetch_json, sleep=time.sleep, polls: int = 12
) -> None:
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug)
        or not re.fullmatch(r"[0-9a-f]{40}", expected["commit"])
        or not run.isdecimal()
        or not attempt.isdecimal()
    ):
        raise ValueError("Invalid delivery attribution")
    base = f"https://api.codecov.io/api/v2/github/{slug.split('/')[0]}/repos/{slug.split('/')[1]}"
    url = f"{base}/commits/{expected['commit']}/"
    deadline = time.monotonic() + 240

    def bounded_fetch(target: str) -> dict:
        if time.monotonic() >= deadline:
            raise ValueError("Codecov verification deadline exceeded")
        return fetch(target)

    for poll in range(polls):
        try:
            commit = bounded_fetch(url + "?format=json")
            uploads = uploads_for(url + "uploads/?format=json", bounded_fetch)
            if delivery_ready(commit, uploads, expected, run, attempt):
                print(f"Verified processed project coverage and test results: {url} (producer {run}.{attempt})")
                return
        except HTTPError as error:
            if error.code not in {404, 429, 500, 502, 503, 504}:
                raise
            print(f"Codecov API not ready (HTTP {error.code})")
        except (URLError, TimeoutError):
            print("Codecov API temporarily unavailable")
        if poll + 1 < polls:
            sleep(10)
    raise ValueError("Timed out waiting for processed Codecov coverage and test results")


def main() -> None:
    temporary = Path(os.environ["RUNNER_TEMP"])
    manifest = temporary / "codecov-expected.json"
    if os.environ["MODE"] == "prepare":
        prepare(
            json.loads((temporary / "codecov-tree.json").read_text()),
            os.environ["SOURCE_SHA"],
            temporary / "reports",
            temporary / "codecov-source",
            manifest,
        )
    else:
        verify(
            json.loads(manifest.read_text()),
            os.environ["GITHUB_REPOSITORY"],
            os.environ["SOURCE_RUN"],
            os.environ["SOURCE_ATTEMPT"],
        )


if __name__ == "__main__":
    main()
