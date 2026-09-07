#!/usr/bin/env python3
"""Delete stale branches across every repo in a GitHub org.

Rules (evaluated per branch, first match wins)
    keep     the repo default branch, `main`, `master`, `develop`, any name listed
             with --protect, and anything GitHub marks as protected
    keep     branches that are the head of an open pull request (deleting them would
             close the PR); reported so a human can decide
    delete   Devin branches whose tip commit is older than --devin-days (30)
    delete   any other branch whose tip commit is older than --stale-days (90)

A branch counts as Devin's when its name starts with `devin/` or the tip commit was
authored/committed by a login containing "devin". Age is the tip commit's committer
date: a branch someone pushes to is "reused" and its clock restarts.

Auth
    GITHUB_MIRROR_PAT (Contents write on the org), else GH_TOKEN / GITHUB_TOKEN, else
    `gh auth token`.

Examples
    scripts/prune_branches.py                       # dry run over codev-workshops
    scripts/prune_branches.py --apply               # actually delete
    scripts/prune_branches.py --repo angular2-hn    # a single repo
    scripts/prune_branches.py --json report.json    # machine-readable summary
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
ALWAYS_KEEP = {"main", "master", "develop"}
DEVIN_NAME = re.compile(r"^devin[/-]", re.IGNORECASE)
DEVIN_LOGIN = re.compile(r"devin", re.IGNORECASE)


def gh_cli_token() -> str:
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def tokens() -> list[str]:
    """Deletion token first, then any ambient token as a read fallback.

    A fine-grained PAT without `Pull requests: read` gets 403 on the pulls endpoint for
    some repos; the ambient (gh CLI / GH_TOKEN) identity can usually still read them.
    """
    seen, out = set(), []
    for tk in (os.environ.get("GITHUB_MIRROR_PAT"), os.environ.get("GH_TOKEN"),
               os.environ.get("GITHUB_TOKEN"), gh_cli_token()):
        if tk and tk not in seen:
            seen.add(tk)
            out.append(tk)
    if not out:
        sys.exit("No GitHub token: set GITHUB_MIRROR_PAT/GH_TOKEN or authenticate the gh CLI.")
    return out


class GitHub:
    def __init__(self, tks: list[str]):
        self.tk, self.fallbacks = tks[0], tks[1:]

    def call(self, path: str, method: str = "GET", body: dict | None = None, tk: str = ""):
        req = urllib.request.Request(
            f"{API}/{path.lstrip('/')}",
            method=method,
            data=json.dumps(body).encode() if body else None,
            headers={
                "Authorization": f"Bearer {tk or self.tk}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "workshop-branch-prune",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def graphql(self, query: str, variables: dict) -> dict:
        res = self.call("graphql", "POST", {"query": query, "variables": variables})
        if res.get("errors"):
            raise RuntimeError(json.dumps(res["errors"])[:300])
        return res["data"]

    def all(self, path: str, tk: str = "") -> list[dict]:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            chunk = self.call(f"{path}{sep}per_page=100&page={page}", tk=tk)
            if not chunk:
                return out
            out.extend(chunk)
            page += 1

    def all_any_token(self, path: str) -> list[dict]:
        for tk in (self.tk, *self.fallbacks):
            try:
                return self.all(path, tk=tk)
            except urllib.error.HTTPError as exc:
                if exc.code not in (401, 403, 404):
                    raise
        raise PermissionError(f"no token can read {path}")


BRANCHES_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        branchProtectionRule { id }
        target { ... on Commit {
          committedDate
          author { name email user { login } }
          committer { name email user { login } }
        } }
      }
    }
  }
}"""


def is_devin(branch: str, commit: dict) -> bool:
    if DEVIN_NAME.match(branch):
        return True
    for role in ("author", "committer"):
        who = commit.get(role) or {}
        login = (who.get("user") or {}).get("login") or ""
        if any(DEVIN_LOGIN.search(s or "") for s in (login, who.get("name"), who.get("email"))):
            return True
    return False


def branches(gh: GitHub, org: str, name: str) -> list[dict]:
    out, after = [], None
    while True:
        refs = gh.graphql(BRANCHES_QUERY, {"owner": org, "name": name, "after": after})["repository"]["refs"]
        out.extend(refs["nodes"])
        if not refs["pageInfo"]["hasNextPage"]:
            return out
        after = refs["pageInfo"]["endCursor"]


def scan_repo(gh: GitHub, org: str, repo: dict, args, now: datetime) -> list[dict]:
    name = repo["name"]
    default = repo["default_branch"]
    protect = ALWAYS_KEEP | set(args.protect) | {default}
    try:
        open_pr_heads = {pr["head"]["ref"] for pr in gh.all_any_token(f"repos/{org}/{name}/pulls?state=open")}
    except PermissionError:
        open_pr_heads = None
    return [classify(br, default, protect, open_pr_heads, args.devin_days, args.stale_days, now) | {"repo": name}
            for br in branches(gh, org, name)]


def classify(br: dict, default: str, protect: set[str], open_pr_heads: set[str] | None,
             devin_days: int, stale_days: int, now: datetime) -> dict:
    b = br["name"]
    row = dict(branch=b, action="keep", reason="", age_days=None, devin=False)
    if b in protect:
        row["reason"] = "default branch" if b == default else "protected name"
    elif br.get("branchProtectionRule"):
        row["reason"] = "github branch protection"
    else:
        commit = br["target"]
        tip = datetime.fromisoformat(commit["committedDate"].replace("Z", "+00:00"))
        age = (now - tip).days
        devin = is_devin(b, commit)
        limit = devin_days if devin else stale_days
        row.update(age_days=age, devin=devin)
        if open_pr_heads is None:
            row["reason"] = "open-PR status unknown (token cannot list PRs)"
        elif b in open_pr_heads:
            row["reason"] = "head of an open PR"
        elif age > limit:
            row.update(action="delete", reason=f"{'devin' if devin else 'user'} branch idle {age}d > {limit}d")
        else:
            row["reason"] = f"active ({age}d <= {limit}d)"
    return row


def report_path(raw: str) -> Path:
    """`--json` must stay inside the working directory."""
    root = Path.cwd().resolve()
    path = (root / raw).resolve()
    if root not in (path, *path.parents):
        sys.exit(f"--json must be a path under {root}")
    return path


def delete(gh: GitHub, org: str, row: dict) -> str:
    ref = urllib.parse.quote(row["branch"], safe="")
    try:
        gh.call(f"repos/{org}/{row['repo']}/git/refs/heads/{ref}", "DELETE")
        return "deleted"
    except urllib.error.HTTPError as exc:
        return f"failed: HTTP {exc.code}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", default="codev-workshops")
    ap.add_argument("--repo", action="append", default=[], help="limit to these repos (repeatable)")
    ap.add_argument("--devin-days", type=int, default=30)
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--protect", action="append", default=[], help="extra branch names to keep (repeatable)")
    ap.add_argument("--apply", action="store_true", help="delete; without it only report")
    ap.add_argument("--json", help="write the full per-branch report here")
    ap.add_argument("-v", "--verbose", action="store_true", help="log each repo as it is scanned")
    args = ap.parse_args()

    gh = GitHub(tokens())
    now = datetime.now(timezone.utc)
    repos = [r for r in gh.all(f"orgs/{args.org}/repos?type=all") if not r["archived"]]
    if args.repo:
        repos = [r for r in repos if r["name"] in set(args.repo)]

    rows: list[dict] = []
    for repo in sorted(repos, key=lambda r: r["name"]):
        if args.verbose:
            print(f"scanning {repo['name']}", file=sys.stderr)
        try:
            rows.extend(scan_repo(gh, args.org, repo, args, now))
        except (urllib.error.URLError, RuntimeError, OSError) as exc:
            rows.append(dict(repo=repo["name"], branch="*", action="error", reason=str(exc)[:120], age_days=None, devin=False))

    to_delete = [r for r in rows if r["action"] == "delete"]
    for r in to_delete:
        r["result"] = delete(gh, args.org, r) if args.apply else "dry-run"
        print(f"{r['result']:>9}  {r['repo']}:{r['branch']}  ({r['reason']})")

    held = [r for r in rows if r["action"] == "keep" and r["reason"].startswith(("head of an open PR", "open-PR status unknown"))
            and r["age_days"] is not None and r["age_days"] > (args.devin_days if r["devin"] else args.stale_days)]
    for r in held:
        print(f"     held  {r['repo']}:{r['branch']}  (stale {r['age_days']}d, {r['reason']})")
    for r in rows:
        if r["action"] == "error":
            print(f"    error  {r['repo']}  ({r['reason']})")

    mode = "deleted" if args.apply else "would delete"
    print(f"\n{len(repos)} repos, {len(rows)} branches scanned: {mode} {len(to_delete)}, "
          f"held {len(held)} (open PR / PR status unknown), kept {len(rows) - len(to_delete) - len(held)}")
    if args.json:
        with open(report_path(args.json), "w") as fh:
            json.dump(dict(org=args.org, apply=args.apply, scanned_at=now.isoformat(), branches=rows), fh, indent=2)
    failed = [r for r in to_delete if r.get("result", "").startswith("failed")]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
