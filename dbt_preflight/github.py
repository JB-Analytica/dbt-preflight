"""Post the comment to the pull request, updating the previous one instead of stacking."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from dbt_preflight.report import MARKER

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def pull_request_number() -> int | None:
    """The PR number from the Actions event payload, if this is a pull_request run."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).exists():
        return None
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except ValueError:
        return None
    pr = event.get("pull_request") or {}
    number = pr.get("number") or event.get("number")
    return int(number) if number else None


def _request(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode()
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        hint = ""
        if exc.code == 403 and method != "GET":
            hint = (
                " The token cannot write to this pull request. That is normal for a pull request "
                "from a fork (GitHub hands it a read-only token) and otherwise means the workflow "
                "lacks `pull-requests: write`. The report is still in the job summary."
            )
        raise GitHubError(f"GitHub API {method} {url} returned {exc.code}: {detail}{hint}") from exc


def post_or_update_comment(repo: str, pr_number: int, body: str, token: str) -> str:
    """Create the preflight comment on the PR, or edit the existing one. Returns its URL."""
    existing_id: int | None = None
    page = 1
    while True:
        status, comments = _request(
            "GET", f"{API}/repos/{repo}/issues/{pr_number}/comments?per_page=100&page={page}", token
        )
        if not isinstance(comments, list) or not comments:
            break
        for c in comments:
            if MARKER in (c.get("body") or ""):
                existing_id = int(c["id"])
                break
        if existing_id is not None or len(comments) < 100:
            break
        page += 1

    if existing_id is not None:
        _, updated = _request(
            "PATCH", f"{API}/repos/{repo}/issues/comments/{existing_id}", token, {"body": body}
        )
        return str(updated.get("html_url", "")) if isinstance(updated, dict) else ""

    _, created = _request(
        "POST", f"{API}/repos/{repo}/issues/{pr_number}/comments", token, {"body": body}
    )
    return str(created.get("html_url", "")) if isinstance(created, dict) else ""
