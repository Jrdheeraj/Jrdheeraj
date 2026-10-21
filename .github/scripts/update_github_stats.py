#!/usr/bin/env python3
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

API_BASE = "https://api.github.com"
START_MARKER = "<!-- GITHUB_STATS:START -->"
END_MARKER = "<!-- GITHUB_STATS:END -->"
README_PATH = "README.md"
MAX_RETRIES = 3


def build_headers(token: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-stats-updater",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _read_error_message(error: urllib.error.HTTPError) -> str:
    try:
        body = error.read().decode("utf-8")
    except Exception:
        return ""
    try:
        return str(json.loads(body).get("message", ""))
    except Exception:
        return ""


def request_json(method: str, url: str, headers: dict, payload: dict | None = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        request = urllib.request.Request(url=url, method=method, headers=headers, data=body)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read().decode("utf-8")
                return json.loads(data), response.headers
        except urllib.error.HTTPError as error:
            remaining = error.headers.get("X-RateLimit-Remaining")
            reset_at = error.headers.get("X-RateLimit-Reset")
            if error.code in (403, 429) and remaining == "0" and reset_at:
                sleep_for = max(1, int(reset_at) - int(time.time()) + 1)
                print(f"Rate limit reached. Sleeping for {sleep_for}s.")
                time.sleep(sleep_for)
                continue
            message = _read_error_message(error).lower()
            if error.code == 403 and (
                "secondary rate limit" in message or "abuse detection" in message
            ):
                if attempt < MAX_RETRIES:
                    attempt += 1
                    sleep_for = 5 * attempt
                    print(
                        f"Secondary rate limit detected for {url}. "
                        f"Retrying in {sleep_for}s ({attempt}/{MAX_RETRIES})."
                    )
                    time.sleep(sleep_for)
                    continue
            if error.code >= 500 and attempt < MAX_RETRIES:
                attempt += 1
                sleep_for = 2 * attempt
                print(
                    f"GitHub API temporary error {error.code} for {url}. "
                    f"Retrying in {sleep_for}s ({attempt}/{MAX_RETRIES})."
                )
                time.sleep(sleep_for)
                continue
            raise RuntimeError(
                f"GitHub API request failed ({method} {url}): HTTP {error.code} {error.reason}"
            ) from error


def fetch_paginated(url: str, headers: dict):
    results = []
    page = 1
    while True:
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}per_page=100&page={page}"
        data, _ = request_json("GET", page_url, headers)
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
        page += 1
    return results


def fetch_public_repositories(owner: str, headers: dict):
    url = f"{API_BASE}/users/{owner}/repos?type=public&sort=updated"
    return fetch_paginated(url, headers)


def count_commits(owner: str, repo_name: str, headers: dict, since: str | None = None) -> int:
    total = 0
    page = 1
    while True:
        query = {
            "author": owner,
            "per_page": 100,
            "page": page,
        }
        if since:
            query["since"] = since
        query_string = urllib.parse.urlencode(query)
        url = f"{API_BASE}/repos/{owner}/{repo_name}/commits?{query_string}"
        try:
            data, _ = request_json("GET", url, headers)
        except urllib.error.HTTPError as error:
            if error.code in (409, 422):
                return total
            raise
        if not data:
            break
        total += len(data)
        if len(data) < 100:
            break
        page += 1
    return total


def count_search(query: str, headers: dict) -> int:
    q = urllib.parse.quote(query, safe="")
    url = f"{API_BASE}/search/issues?q={q}&per_page=1"
    data, _ = request_json("GET", url, headers)
    if "total_count" not in data:
        raise RuntimeError(f"GitHub search response missing total_count for query: {query}")
    return int(data["total_count"])


def count_commit_search(owner: str, headers: dict) -> int | None:
    query = urllib.parse.quote(f"author:{owner} is:public", safe="")
    url = f"{API_BASE}/search/commits?q={query}&per_page=1"
    commit_headers = dict(headers)
    commit_headers["Accept"] = "application/vnd.github.cloak-preview+json"
    try:
        data, _ = request_json("GET", url, commit_headers)
    except RuntimeError as error:
        print(f"Commit search API unavailable, falling back to repository commit scan: {error}")
        return None
    if "total_count" not in data:
        return None
    return int(data["total_count"])


def contribution_total_from_graphql(owner: str, headers: dict, year: int) -> int | None:
    if "Authorization" not in headers:
        return None
    query = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
      }
    }
  }
}
"""
    payload = {
        "query": query,
        "variables": {
            "login": owner,
            "from": f"{year}-01-01T00:00:00Z",
            "to": f"{year}-12-31T23:59:59Z",
        },
    }
    try:
        data, _ = request_json("POST", f"{API_BASE}/graphql", headers, payload)
    except RuntimeError as error:
        print(f"GraphQL contribution query failed, using fallback: {error}")
        return None

    if data.get("errors"):
        print(f"GraphQL returned errors, using fallback: {data.get('errors')}")
        return None
    user_data = data.get("data", {}).get("user")
    if not user_data:
        return None
    return int(
        user_data.get("contributionsCollection", {})
        .get("contributionCalendar", {})
        .get("totalContributions", 0)
    )


def encode_badge_value(value: int) -> str:
    return urllib.parse.quote(str(f"{value:,}"), safe="")


def make_badge(label: str, value: int, color: str, logo: str, logo_color: str, alt: str) -> str:
    encoded_label = urllib.parse.quote(label, safe="")
    encoded_value = encode_badge_value(value)
    return (
        f'  <img src="https://img.shields.io/badge/{encoded_label}-{encoded_value}-{color}'
        f'?style=for-the-badge&logo={logo}&logoColor={logo_color}" alt="{alt}" />'
    )


def replace_generated_block(readme_text: str, generated_body: str) -> str:
    pattern = re.compile(
        rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
        flags=re.DOTALL,
    )
    replacement = f"{START_MARKER}\n{generated_body}\n  {END_MARKER}"
    if not pattern.search(readme_text):
        raise RuntimeError("Generated stats markers were not found in README.md")
    return pattern.sub(replacement, readme_text, count=1)


def compute_commit_totals(
    owner: str, repositories: list[dict[str, Any]], headers: dict, since_start_of_year: str
) -> tuple[int, int, str]:
    total_commits_from_search = count_commit_search(owner, headers)
    total_commits_from_repo_scan = 0
    yearly_commits = 0
    for repo in repositories:
        repo_name = repo.get("name")
        if not repo_name:
            continue
        total_commits_from_repo_scan += count_commits(owner, repo_name, headers)
        yearly_commits += count_commits(owner, repo_name, headers, since=since_start_of_year)

    if total_commits_from_search is None:
        return total_commits_from_repo_scan, yearly_commits, "repo_scan"
    return (
        max(total_commits_from_search, total_commits_from_repo_scan),
        yearly_commits,
        "max(search, repo_scan)",
    )


def main():
    owner = os.environ.get("STATS_USERNAME") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "Jrdheeraj"
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = build_headers(token)
    year = datetime.now(timezone.utc).year
    since_start_of_year = f"{year}-01-01T00:00:00Z"

    repositories = fetch_public_repositories(owner, headers)
    total_stars = sum(int(repo.get("stargazers_count", 0)) for repo in repositories)

    total_commits, yearly_commits, commits_source = compute_commit_totals(
        owner, repositories, headers, since_start_of_year
    )

    total_pull_requests = count_search(f"type:pr author:{owner} is:public", headers)
    total_issues = count_search(f"type:issue author:{owner} is:public", headers)

    yearly_contributions = contribution_total_from_graphql(owner, headers, year)
    yearly_label = f"Contributions ({year})"
    yearly_alt = "Total contributions in the current year"
    if yearly_contributions is None:
        yearly_contributions = yearly_commits
        yearly_label = f"Contributions ({year}, Best Effort)"
        yearly_alt = "Best-effort current year contributions based on public commit activity"

    generated_lines = [
        "  <!-- Auto-generated by .github/scripts/update_github_stats.py; do not edit manually. -->",
        "  <!-- Total commits use a best-effort public API method and may undercount in edge cases. -->",
        "  <!-- Static README content cannot update every second; this block refreshes on workflow schedule/manual run. -->",
        make_badge(
            "Total Stars Earned",
            total_stars,
            "0A0F1C",
            "github",
            "00D4FF",
            "Total stars earned across public repositories",
        ),
        "  <br /><br />",
        make_badge(
            "Total Commits (Best Effort)",
            total_commits,
            "111827",
            "git",
            "7B2CBF",
            "Total commits authored across public repositories (best effort)",
        ),
        "  <br /><br />",
        make_badge(
            "Total Pull Requests",
            total_pull_requests,
            "181717",
            "github",
            "00D4FF",
            "Total pull requests authored across repositories",
        ),
        "  <br /><br />",
        make_badge(
            "Total Issues (Excl. PRs)",
            total_issues,
            "0A0F1C",
            "github",
            "7B2CBF",
            "Total issues authored across repositories excluding pull requests",
        ),
        "  <br /><br />",
        make_badge(
            yearly_label,
            yearly_contributions,
            "111827",
            "github",
            "00D4FF",
            yearly_alt,
        ),
        "  <br /><br />",
        "  <sub>Cross-repository totals refresh daily via workflow or manual dispatch. "
        "Repository badges can be live; static README totals are scheduled.</sub>",
    ]
    generated_body = "\n".join(generated_lines)

    with open(README_PATH, "r", encoding="utf-8") as readme_file:
        current_readme = readme_file.read()

    updated_readme = replace_generated_block(current_readme, generated_body)
    with open(README_PATH, "w", encoding="utf-8") as readme_file:
        readme_file.write(updated_readme)

    print(
        "Updated README stats block:",
        json.dumps(
            {
                "owner": owner,
                "repository_count": len(repositories),
                "stars": total_stars,
                "commits_best_effort": total_commits,
                "commits_source": commits_source,
                "pull_requests": total_pull_requests,
                "issues_excluding_prs": total_issues,
                "year": year,
                "yearly_contributions": yearly_contributions,
            }
        ),
    )


if __name__ == "__main__":
    main()
