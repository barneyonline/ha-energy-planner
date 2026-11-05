"""Check this producer attempt through Codecov's anonymous processing API.

The workflow loads this check from its immutable default-branch revision.
"""

from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import urlopen


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


if __name__ == "__main__":
    verify(
        os.environ["SOURCE_SHA"],
        os.environ["GITHUB_REPOSITORY"],
        os.environ["SOURCE_RUN"],
        os.environ["SOURCE_ATTEMPT"],
    )
