"""Supply an inert producer inventory and check Codecov's public processing API.

The workflow loads this helper from its immutable default-branch revision.
Candidate source is represented only by Git tree metadata, never source blobs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import urlopen


def prepare(tree: dict, sha: str, temporary: Path) -> None:
    """Make the file inventory the uploader needs without checking out PR code."""
    if tree.get("commit") != sha or tree.get("truncated") is not False or len(tree["tree"]) > 50000:
        raise ValueError("Incomplete or mismatched producer tree")
    source = temporary / "codecov-source"
    source.mkdir()
    (temporary / "codecov-work").mkdir()  # Wrapper downloads must stay outside root_dir.
    count = 0
    for entry in tree["tree"]:
        name = entry["path"]
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or path.as_posix() != name
            or "\\" in name
            or any(part in {"..", ".git"} for part in path.parts)
            or any(ord(char) < 32 for char in name)
        ):
            raise ValueError("Invalid producer source path")
        if entry["type"] != "blob" or entry["mode"] not in {"100644", "100755"}:
            continue
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)
        count += 1
    if not count:
        raise ValueError("Empty producer inventory")
    print(f"Prepared {count} inert producer source paths")


def fetch_json(url: str) -> dict:
    # No GitHub/OIDC/Codecov credentials are sent to this public read-only API.
    if not url.startswith("https://api.codecov.io/api/v2/github/"):
        raise ValueError("Unexpected Codecov API URL")
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def uploads_for(url: str, fetch) -> list[dict]:
    uploads = []
    for _ in range(20):
        page = fetch(url)
        uploads.extend(page["results"])
        if not page.get("next"):
            return uploads
        url = urljoin(url, page["next"])
    raise ValueError("Codecov upload pagination did not finish")


def delivery_ready(commit: dict, uploads: list[dict], sha: str, run: str, attempt: str) -> bool:
    # The API leaves build_code and JUnit name null. Match the kind-specific
    # identifiers we supplied to the documented action inputs instead.
    coverage = [item for item in uploads if item.get("name") == f"coverage-{run}-{attempt}"]
    junit = [item for item in uploads if item.get("job_code") == f"junit-{run}-{attempt}"]
    if not coverage or not junit:
        return False
    if len(coverage) != 1 or len(junit) != 1 or any(item.get("state") == "error" for item in coverage + junit):
        raise ValueError("Codecov rejected this attempt or returned duplicate uploads")
    coverage, junit = coverage[0], junit[0]
    if coverage.get("state") != "merged" or junit.get("state") != "processed":
        return False
    if not junit.get("storage_path", "").startswith("test_results/v1/raw/"):
        raise ValueError("Expected processed test results")
    if commit.get("commitid") != sha or commit.get("state") == "error":
        raise ValueError("Wrong or failed Codecov commit")
    if commit.get("state") != "complete":
        return False
    # The current upload must itself contain coverage; an older good attempt or
    # a green no-op patch status cannot supply evidence for this attempt.
    for totals in (coverage.get("totals") or {}, commit.get("totals") or {}):
        if totals.get("files", 0) <= 0 or totals.get("lines", 0) <= 0:
            raise ValueError("Codecov processed empty coverage")
    return True


def verify(sha: str, slug: str, run: str, attempt: str, *, fetch=fetch_json, sleep=time.sleep) -> None:
    owner, repo = slug.split("/")
    url = f"https://api.codecov.io/api/v2/github/{owner}/repos/{repo}/commits/{sha}/"
    deadline = time.monotonic() + 240

    def bounded_fetch(target: str) -> dict:
        if time.monotonic() >= deadline:
            raise ValueError("Codecov verification deadline exceeded")
        return fetch(target)

    for poll in range(12):
        try:
            commit = bounded_fetch(url + "?format=json")
            uploads = uploads_for(url + "uploads/?format=json", bounded_fetch)
            if delivery_ready(commit, uploads, sha, run, attempt):
                print(f"Verified merged coverage and processed JUnit for {sha}, producer {run}.{attempt}")
                return
        except HTTPError as error:
            error.close()
            if error.code not in {404, 429, 500, 502, 503, 504}:
                raise
        except (URLError, TimeoutError):
            pass
        if poll < 11:
            sleep(10)
    raise ValueError("Timed out waiting for Codecov processing")


def main() -> None:
    temporary = Path(os.environ["RUNNER_TEMP"])
    sha = os.environ["SOURCE_SHA"]
    if os.environ["MODE"] == "prepare":
        prepare(json.loads((temporary / "codecov-tree.json").read_text()), sha, temporary)
    else:
        verify(sha, os.environ["GITHUB_REPOSITORY"], os.environ["SOURCE_RUN"], os.environ["SOURCE_ATTEMPT"])


if __name__ == "__main__":
    main()
