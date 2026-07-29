"""Computes GitHub activity stats and writes them into light_mode.svg / dark_mode.svg."""
import hashlib
import os
from datetime import datetime, timezone

import requests
from dateutil.relativedelta import relativedelta
from lxml import etree

API_URL = "https://api.github.com/graphql"
SVG_NS = {"svg": "http://www.w3.org/2000/svg"}
SVG_FILES = ["light_mode.svg", "dark_mode.svg"]
CACHE_DIR = "cache"
BIRTH_YEAR_FALLBACK = 2020  # only used if user creation date can't be read


def run_query(token, query, variables):
    resp = requests.post(
        API_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


USER_QUERY = """
query($login: String!) {
  user(login: $login) {
    id
    createdAt
    followers { totalCount }
  }
}
"""


def get_user_info(token, login):
    data = run_query(token, USER_QUERY, {"login": login})["user"]
    return data["id"], data["createdAt"], data["followers"]["totalCount"]


def format_age(created_at):
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    delta = relativedelta(datetime.now(timezone.utc), created)
    return f"{delta.years}y {delta.months}m {delta.days}d"


REPO_STAR_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER], isFork: false) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { stargazerCount }
    }
  }
}
"""


def get_star_total(token, login):
    stars, cursor = 0, None
    while True:
        data = run_query(token, REPO_STAR_QUERY, {"login": login, "cursor": cursor})
        repos = data["user"]["repositories"]
        stars += sum(node["stargazerCount"] for node in repos["nodes"])
        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]
    return stars


REPO_LIST_QUERY = """
query($login: String!, $id: ID!, $cursor: String) {
  user(login: $login) {
    repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER], isFork: false) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        defaultBranchRef {
          target {
            ... on Commit {
              history(author: { id: $id }) { totalCount }
            }
          }
        }
      }
    }
  }
}
"""


def get_repo_list(token, login, user_id):
    repos, cursor, total_count = [], None, 0
    while True:
        data = run_query(token, REPO_LIST_QUERY, {"login": login, "id": user_id, "cursor": cursor})
        page = data["user"]["repositories"]
        total_count = page["totalCount"]
        for node in page["nodes"]:
            branch = node["defaultBranchRef"]
            my_commits = branch["target"]["history"]["totalCount"] if branch else 0
            repos.append((node["nameWithOwner"], my_commits))
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return repos, total_count


COMMIT_HISTORY_QUERY = """
query($owner: String!, $name: String!, $id: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $cursor, author: { id: $id }) {
            pageInfo { hasNextPage endCursor }
            edges { node { additions deletions } }
          }
        }
      }
    }
  }
}
"""


def walk_repo_loc(token, name_with_owner, user_id):
    owner, name = name_with_owner.split("/", 1)
    additions = deletions = 0
    cursor = None
    while True:
        data = run_query(
            token, COMMIT_HISTORY_QUERY,
            {"owner": owner, "name": name, "id": user_id, "cursor": cursor},
        )
        branch = data["repository"]["defaultBranchRef"]
        if not branch:
            break
        history = branch["target"]["history"]
        for edge in history["edges"]:
            additions += edge["node"]["additions"]
            deletions += edge["node"]["deletions"]
        if not history["pageInfo"]["hasNextPage"]:
            break
        cursor = history["pageInfo"]["endCursor"]
    return additions, deletions


def load_cache(login):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, hashlib.sha256(login.encode()).hexdigest() + ".txt")
    cache = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(" ")
                if len(parts) != 4:
                    continue
                repo, commits, add, dele = parts
                cache[repo] = (int(commits), int(add), int(dele))
    return path, cache


def save_cache(path, cache):
    with open(path, "w", encoding="utf-8") as f:
        for repo in sorted(cache):
            commits, add, dele = cache[repo]
            f.write(f"{repo} {commits} {add} {dele}\n")


def compute_loc_and_commits(token, login, user_id, repos):
    cache_path, cache = load_cache(login)
    total_commits = total_add = total_del = 0
    for name_with_owner, my_commits in repos:
        cached = cache.get(name_with_owner)
        if cached and cached[0] == my_commits:
            _, add, dele = cached
        elif my_commits == 0:
            add, dele = 0, 0
            cache[name_with_owner] = (0, 0, 0)
        else:
            add, dele = walk_repo_loc(token, name_with_owner, user_id)
            cache[name_with_owner] = (my_commits, add, dele)
        total_commits += my_commits
        total_add += add
        total_del += dele
    save_cache(cache_path, cache)
    return total_commits, total_add, total_del


LANGUAGE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER], isFork: false) {
      pageInfo { hasNextPage endCursor }
      nodes {
        languages(first: 10, orderBy: { field: SIZE, direction: DESC }) {
          edges { size node { name } }
        }
      }
    }
  }
}
"""

LANGUAGE_LIMIT = 6


def get_top_languages(token, login, limit=LANGUAGE_LIMIT):
    sizes, cursor = {}, None
    while True:
        data = run_query(token, LANGUAGE_QUERY, {"login": login, "cursor": cursor})
        page = data["user"]["repositories"]
        for node in page["nodes"]:
            for edge in node["languages"]["edges"]:
                name = edge["node"]["name"]
                sizes[name] = sizes.get(name, 0) + edge["size"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    ranked = sorted(sizes.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [name for name, _ in ranked]


STATIC_STACK = ".NET Framework, .NET Core, ASP.NET, Entity Framework, React"

# Character column where every row's value ends, so values stay right-aligned
# with a dot leader of whatever length closes the gap from the label.
RIGHT_COL = 80


def render_row(tree, row_id, label, value):
    """value is either a plain string, or a list of (text, css_class) tuples
    for rows that need multiple colors (e.g. Lines of Code)."""
    nodes = tree.xpath(f'//svg:text[@id="{row_id}"]', namespaces=SVG_NS)
    if not nodes:
        return
    row = nodes[0]
    row.text = None
    for child in list(row):
        row.remove(child)

    parts = [(value, "value")] if isinstance(value, str) else value
    value_len = sum(len(text) for text, _ in parts)

    label_part = f". {label}:"
    dots = max(3, RIGHT_COL - len(label_part) - value_len - 2)

    label_span = etree.SubElement(row, "{http://www.w3.org/2000/svg}tspan")
    label_span.set("class", "label")
    label_span.text = label_part

    dots_span = etree.SubElement(row, "{http://www.w3.org/2000/svg}tspan")
    dots_span.set("class", "dots")
    dots_span.text = ("." * dots) + " "

    for text, css_class in parts:
        span = etree.SubElement(row, "{http://www.w3.org/2000/svg}tspan")
        span.set("class", css_class)
        span.text = text


def main():
    token = os.environ["ACCESS_TOKEN"]
    login = os.environ["USER_NAME"]

    user_id, created_at, followers = get_user_info(token, login)
    star_total = get_star_total(token, login)
    repos, repo_count = get_repo_list(token, login, user_id)
    commits, additions, deletions = compute_loc_and_commits(token, login, user_id, repos)
    languages = get_top_languages(token, login)

    rows = [
        ("row_age", "Uptime", format_age(created_at)),
        ("row_repo", "Repositories", f"{repo_count:,}"),
        ("row_commit", "Commits", f"{commits:,}"),
        ("row_loc", "Lines of Code", [
            (f"{additions:,}++", "loc-add"),
            (", ", "value"),
            (f"{deletions:,}--", "loc-del"),
        ]),
        ("row_star", "Stars Earned", f"{star_total:,}"),
        ("row_follower", "Followers", f"{followers:,}"),
        ("row_languages", "Languages", ", ".join(languages) if languages else "n/a"),
        ("row_stack", "Stack", STATIC_STACK),
    ]

    for svg_file in SVG_FILES:
        tree = etree.parse(svg_file)
        for row_id, label, value in rows:
            render_row(tree, row_id, label, value)
        tree.write(svg_file, xml_declaration=False, encoding="utf-8")


if __name__ == "__main__":
    main()
